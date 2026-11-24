"""The ledger's rules, kept by the database itself, whoever writes to it:

* transfers, postings and holders are append-only (core.append_only);
* an account and a book are never deleted, and an account's book, holder, counterpart and bank
  never change; a closed account stays closed;
* a posting continues its account's chain: it comes after the account's last posting, its
  balance_after is that posting's plus its amount, and its prev_hash is that posting's row_hash.
  The account's row is locked first, so two writers to one account take turns;
* a transfer has exactly two postings, one out and one in, summing to 0. Checked at commit, or
  earlier where the ledger asks (SET CONSTRAINTS … IMMEDIATE), since both postings are written
  after the transfer.
"""

from django.db import migrations

from core.append_only import trigger_sql

APPEND_ONLY, APPEND_ONLY_REVERSE = trigger_sql(
    "credit_append_only", ("credit_holder", "credit_transfer", "credit_posting"), "The GAHK-konto ledger"
)

RULES = [
    """
    CREATE FUNCTION credit_account_fixed() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'GAHK-konto: accounts are never deleted (account %)', OLD.id;
        END IF;
        IF NEW.book_id IS DISTINCT FROM OLD.book_id
           OR NEW.holder_id IS DISTINCT FROM OLD.holder_id
           OR NEW.counterpart_book_id IS DISTINCT FROM OLD.counterpart_book_id
           OR NEW.bank_reference IS DISTINCT FROM OLD.bank_reference THEN
            RAISE EXCEPTION 'GAHK-konto: an account''s book, holder, counterpart and bank never change (account %)', OLD.id;
        END IF;
        IF OLD.closed_at IS NOT NULL AND NEW.closed_at IS DISTINCT FROM OLD.closed_at THEN
            RAISE EXCEPTION 'GAHK-konto: a closed account stays closed (account %)', OLD.id;
        END IF;
        RETURN NEW;
    END;
    $$
    """,
    (
        "CREATE TRIGGER credit_creditaccount_fixed BEFORE UPDATE OR DELETE ON credit_creditaccount "
        "FOR EACH ROW EXECUTE FUNCTION credit_account_fixed()"
    ),
    """
    CREATE FUNCTION credit_book_kept() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'GAHK-konto: books are never deleted (book %)', OLD.id;
    END;
    $$
    """,
    "CREATE TRIGGER credit_book_kept BEFORE DELETE ON credit_book FOR EACH ROW EXECUTE FUNCTION credit_book_kept()",
    """
    CREATE FUNCTION credit_posting_links() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
        closed timestamptz;
        prev_id bigint;
        prev_balance bigint;
        prev_row bytea;
    BEGIN
        SELECT closed_at INTO closed FROM credit_creditaccount WHERE id = NEW.account_id FOR UPDATE;
        IF closed IS NOT NULL THEN
            RAISE EXCEPTION 'GAHK-konto: account % is closed', NEW.account_id;
        END IF;
        SELECT id, balance_after, row_hash INTO prev_id, prev_balance, prev_row
            FROM credit_posting WHERE account_id = NEW.account_id ORDER BY id DESC LIMIT 1;
        IF NOT FOUND THEN
            prev_id := 0;
            prev_balance := 0;
            prev_row := decode(repeat('00', 32), 'hex');
        END IF;
        IF NEW.id <= prev_id THEN
            RAISE EXCEPTION 'GAHK-konto: posting % would come before posting % of account %', NEW.id, prev_id, NEW.account_id;
        END IF;
        IF NEW.balance_after <> prev_balance + NEW.amount_ore THEN
            RAISE EXCEPTION 'GAHK-konto: account % would go from % to % with %', NEW.account_id, prev_balance, NEW.balance_after, NEW.amount_ore;
        END IF;
        IF NEW.prev_hash <> prev_row OR length(NEW.row_hash) <> 32 THEN
            RAISE EXCEPTION 'GAHK-konto: posting does not continue the hash chain of account %', NEW.account_id;
        END IF;
        RETURN NEW;
    END;
    $$
    """,
    (
        "CREATE TRIGGER credit_posting_links BEFORE INSERT ON credit_posting "
        "FOR EACH ROW EXECUTE FUNCTION credit_posting_links()"
    ),
    """
    CREATE FUNCTION credit_transfer_balanced() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
        tid bigint;
        n integer;
        total bigint;
        outgoing integer;
    BEGIN
        IF TG_TABLE_NAME = 'credit_transfer' THEN
            tid := NEW.id;
        ELSE
            tid := NEW.transfer_id;
        END IF;
        SELECT count(*), coalesce(sum(amount_ore), 0), count(*) FILTER (WHERE amount_ore < 0)
            INTO n, total, outgoing
            FROM credit_posting WHERE transfer_id = tid;
        IF n <> 2 OR total <> 0 OR outgoing <> 1 THEN
            RAISE EXCEPTION 'GAHK-konto: transfer % needs exactly two postings, one out and one in, summing to 0 (has %, sum %)', tid, n, total;
        END IF;
        RETURN NULL;
    END;
    $$
    """,
    (
        "CREATE CONSTRAINT TRIGGER credit_transfer_balanced AFTER INSERT ON credit_transfer "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION credit_transfer_balanced()"
    ),
    (
        "CREATE CONSTRAINT TRIGGER credit_posting_balanced AFTER INSERT ON credit_posting "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION credit_transfer_balanced()"
    ),
]
RULES_REVERSE = [
    "DROP TRIGGER credit_posting_balanced ON credit_posting",
    "DROP TRIGGER credit_transfer_balanced ON credit_transfer",
    "DROP FUNCTION credit_transfer_balanced()",
    "DROP TRIGGER credit_posting_links ON credit_posting",
    "DROP FUNCTION credit_posting_links()",
    "DROP TRIGGER credit_book_kept ON credit_book",
    "DROP FUNCTION credit_book_kept()",
    "DROP TRIGGER credit_creditaccount_fixed ON credit_creditaccount",
    "DROP FUNCTION credit_account_fixed()",
]


class Migration(migrations.Migration):
    dependencies = [("credit", "0001_initial")]

    operations = [
        migrations.RunSQL(APPEND_ONLY, reverse_sql=APPEND_ONLY_REVERSE),
        migrations.RunSQL(RULES, reverse_sql=RULES_REVERSE),
    ]
