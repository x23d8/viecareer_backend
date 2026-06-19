-- Restore production plan pricing after temporary low-price seeds.
-- The type normalization is safe to run repeatedly when existing values are numeric strings.

alter table plans
  alter column monthly_price type numeric(12,2)
  using monthly_price::numeric;

update plans
set monthly_price = case code
  when 'free' then 0
  when 'standard' then 99000
  when 'pro' then 299000
  when 'team' then 1199000
  when 'usage_based' then 29000
  else monthly_price
end
where code in ('free', 'standard', 'pro', 'team', 'usage_based');