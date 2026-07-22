create table if not exists public.codegate_user_access (
    user_id uuid primary key references auth.users(id) on delete cascade,
    tenant_id text not null,
    read_access text[] not null default array['public']::text[],
    updated_at timestamptz not null default now(),
    constraint codegate_read_access_values check (
        read_access <@ array['public', 'internal', 'restricted']::text[]
    )
);

create table if not exists public.codegate_document_permissions (
    user_id uuid not null references public.codegate_user_access(user_id) on delete cascade,
    document_id text not null,
    can_write boolean not null default false,
    updated_at timestamptz not null default now(),
    primary key (user_id, document_id),
    constraint codegate_document_id_format check (
        document_id ~ '^[A-Z][A-Z0-9]*(-[A-Z0-9]+)+$'
        and length(document_id) <= 96
    )
);

alter table public.codegate_user_access enable row level security;
alter table public.codegate_document_permissions enable row level security;

revoke all on public.codegate_user_access from anon, authenticated, public;
revoke all on public.codegate_document_permissions from anon, authenticated, public;
grant select on public.codegate_user_access to supabase_auth_admin;
grant select on public.codegate_document_permissions to supabase_auth_admin;
grant usage on schema public to supabase_auth_admin;

drop policy if exists "auth hook reads user access" on public.codegate_user_access;
create policy "auth hook reads user access"
on public.codegate_user_access
for select
to supabase_auth_admin
using (true);

drop policy if exists "auth hook reads document permissions"
on public.codegate_document_permissions;
create policy "auth hook reads document permissions"
on public.codegate_document_permissions
for select
to supabase_auth_admin
using (true);

create or replace function public.codegate_custom_access_token_hook(event jsonb)
returns jsonb
language plpgsql
stable
set search_path = ''
as $$
declare
    claims jsonb;
    access_row public.codegate_user_access%rowtype;
    writable_documents jsonb;
begin
    claims := event->'claims';

    select *
    into access_row
    from public.codegate_user_access
    where user_id = (event->>'user_id')::uuid;

    if access_row.user_id is null then
        writable_documents := '[]'::jsonb;
    else
        select coalesce(jsonb_agg(document_id order by document_id), '[]'::jsonb)
        into writable_documents
        from (
            select document_id
            from public.codegate_document_permissions
            where user_id = access_row.user_id
              and can_write
            order by document_id
            limit 100
        ) allowed_documents;
    end if;

    claims := jsonb_set(
        claims,
        '{codegate_provisioned}',
        to_jsonb(access_row.user_id is not null),
        true
    );
    claims := jsonb_set(
        claims,
        '{codegate_tenant_id}',
        to_jsonb(coalesce(access_row.tenant_id, 'default')),
        true
    );
    claims := jsonb_set(
        claims,
        '{codegate_read_access}',
        to_jsonb(coalesce(access_row.read_access, array['public']::text[])),
        true
    );
    claims := jsonb_set(
        claims,
        '{codegate_write_document_ids}',
        writable_documents,
        true
    );

    return jsonb_build_object('claims', claims);
end;
$$;

grant execute on function public.codegate_custom_access_token_hook(jsonb)
to supabase_auth_admin;
revoke execute on function public.codegate_custom_access_token_hook(jsonb)
from anon, authenticated, public;
