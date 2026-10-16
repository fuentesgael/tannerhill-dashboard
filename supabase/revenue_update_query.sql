-- =====================================================================================
-- Revenue update: add a revenue batch to the monthly revenue table
-- =====================================================================================
--
-- WHAT THIS DOES, IN PLAIN WORDS
--   1. Takes every row you imported into the staging table, d11_revenue_staging.
--   2. Groups them by month (the "date" column, written like 4/1/2026 or 4/14/2026),
--      and adds up the revenue separately for Amazon and for Shopify.
--   3. For each month in the batch, it updates d10_02_revenue_by_month:
--        - MONTH NOT IN THE TABLE YET -> it ADDS a new row for that month:
--            Amazon and Shopify = this batch's totals, Kickstarter = 0,
--            Kickstarter pledges = 0, Total revenue = Amazon + Shopify,
--            Books = 'Open - not yet closed', updated_at = now.
--        - MONTH ALREADY IN THE TABLE -> it ADDS this batch's totals ON TOP OF the
--            numbers already there (Amazon, Shopify and Total revenue all go up by
--            this batch's amounts) and updated_at is set to now. It never replaces
--            the amounts, and it leaves Books and Kickstarter alone.
--      updated_at is what the dashboard shows as "Data as of". It only changes
--      when this query actually adds or updates a month, never when nothing is staged.
--   4. Empties the staging table, so the same batch can't be counted twice.
--   5. Shows a small table of what it did: each month touched, whether that row was
--      "added" or "updated", the amounts this batch added, and the month's new totals.
--
-- HOW TO USE IT (Supabase dashboard)
--   a. Table Editor -> d11_revenue_staging -> Insert -> Import data from CSV ->
--      pick ONE batch file (e.g. Revenue_Batch_A_2026-04-01_to_2026-04-07.csv).
--   b. SQL Editor -> open this saved query -> Run.
--   c. Check the result table. Then repeat a-b for the next batch file.
--
-- SAFETY BUILT IN
--   - Staging table empty? Nothing happens: zero rows change, no error, and no
--     zero-amount "update" is written.
--   - Run it twice by accident? The second run finds an empty staging table and
--     does nothing, because the first run emptied it.
--   - Any staging row it can't read (a channel other than exactly 'Amazon' or
--     'Shopify', a missing revenue amount, or a date not written like 4/1/2026)
--     stops the whole run with a message saying what's wrong. NOTHING is changed
--     and the staging rows are kept, so you can fix them and run again.
--   - Steps 3 and 4 are one single statement: either all of it happens, or none.
--
-- Requires the unique constraint on "Month" (added in supabase/schema.sql).
-- =====================================================================================


-- STEP A: check every staging row before anything changes. Stops with a clear
-- message if a row can't be read; when the staging table is empty it does nothing.
do $$
declare
  bad text;
  d   text;
begin
  select string_agg(format('date=%L channel=%L revenue=%s', s.date, s.channel, coalesce(s.revenue::text, 'missing')), '; ')
    into bad
  from (
    select * from public.d11_revenue_staging
    where channel is null or channel not in ('Amazon', 'Shopify')
       or revenue is null
       or date is null or trim(date) !~ '^[0-9]{1,2}/[0-9]{1,2}/[0-9]{4}$'
    limit 5
  ) s;
  if bad is not null then
    raise exception 'Revenue update stopped, nothing was changed. These staging rows (first 5 shown) need fixing: %', bad;
  end if;
  -- Dates shaped right but impossible (e.g. 13/40/2026).
  for d in select distinct trim(date) from public.d11_revenue_staging loop
    begin
      perform to_date(d, 'MM/DD/YYYY');
    exception when others then
      raise exception 'Revenue update stopped, nothing was changed. This staging date is not a real date: %', d;
    end;
  end loop;
end $$;


-- STEP B: add the batch to d10_02_revenue_by_month, empty the staging table, and
-- show what was done. All three parts are one statement, so they succeed or fail together.
with
-- B0. Second safety net, in case Step A was skipped: if ANY staging row is unreadable,
--     this statement changes nothing at all (no update, staging rows kept).
all_rows_ok as (
  select not exists (
    select 1 from public.d11_revenue_staging
    where channel is null or channel not in ('Amazon', 'Shopify')
       or revenue is null
       or date is null or trim(date) !~ '^[0-9]{1,2}/[0-9]{1,2}/[0-9]{4}$'
  ) as ok
),
-- B1. The batch, one line per month: Amazon total and Shopify total.
batch as (
  select
    date_trunc('month', to_date(trim(date), 'MM/DD/YYYY'))::date              as month,
    coalesce(sum(revenue) filter (where channel = 'Amazon'), 0)               as amazon,
    coalesce(sum(revenue) filter (where channel = 'Shopify'), 0)              as shopify
  from public.d11_revenue_staging
  where (select ok from all_rows_ok)
  group by 1
),
-- B1b. Which of those months are new (not in the revenue table yet).
batch_new as (
  select b.*,
         not exists (select 1 from public.d10_02_revenue_by_month m where m."Month" = b.month) as is_new
  from batch b
),
-- B2. Add new months, or add on top of months already there.
--     source_row for a new month = the table's current highest source_row + 1
--     (+2 for a second new month in the same batch, and so on; existing months
--     keep their own source_row).
upserted as (
  insert into public.d10_02_revenue_by_month as r
    (source_row, "Month",
     "Amazon (net of refunds)", "Shopify (net of discounts and returns)",
     "Kickstarter (recognized when rewards ship)", "Total revenue",
     "Kickstarter pledges collected, net of fees (cash, not revenue)", "Books", updated_at)
  select
    (select coalesce(max(source_row), 0) from public.d10_02_revenue_by_month)
      + row_number() over (partition by b.is_new order by b.month),
    b.month,
    b.amazon, b.shopify,
    0.00, b.amazon + b.shopify,
    0.00, 'Open - not yet closed',
    now()
  from batch_new b
  order by b.month
  -- Month already exists -> ADD this batch's amounts to what's there; don't replace.
  -- ("excluded" means "the amounts from this batch".)
  on conflict ("Month") do update set
    "Amazon (net of refunds)"                = coalesce(r."Amazon (net of refunds)", 0)                + excluded."Amazon (net of refunds)",
    "Shopify (net of discounts and returns)" = coalesce(r."Shopify (net of discounts and returns)", 0) + excluded."Shopify (net of discounts and returns)",
    "Total revenue"                          = coalesce(r."Total revenue", 0)                          + excluded."Total revenue",
    updated_at                               = now()
  -- xmax = 0 is Postgres' marker for "this row was just inserted" (vs. updated).
  returning r."Month", (r.xmax = 0) as was_added,
            r."Amazon (net of refunds)", r."Shopify (net of discounts and returns)", r."Total revenue", r."Books", r.updated_at
),
-- B3. Empty the staging table (only the rows this run read).
emptied as (
  delete from public.d11_revenue_staging
  where (select ok from all_rows_ok)
  returning 1
)
-- B4. What was done. An empty result means the staging table was empty and nothing changed.
select
  to_char(u."Month", 'Mon YYYY')                                 as month,
  case when u.was_added then 'added (new month)' else 'updated (added on top)' end as action,
  b.amazon                                                       as amazon_added,
  b.shopify                                                      as shopify_added,
  u."Amazon (net of refunds)"                                    as amazon_now,
  u."Shopify (net of discounts and returns)"                     as shopify_now,
  u."Total revenue"                                              as total_revenue_now,
  u."Books"                                                      as books,
  u.updated_at                                                   as updated_at,
  (select count(*) from emptied)                                 as staging_rows_used
from upserted u
join batch b on b.month = u."Month"
order by u."Month";
