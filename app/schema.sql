-- Inspection job-card supply ledger.
--
-- Supply counters are materialised per (card, item).  With a fixed card and
-- item dictionary there are cards x items rows, regardless of how many
-- batches have been received.  Submission and correction each touch only
-- this card's item rows (fixed size), and an item query reads one index
-- entry per card (fixed size).  None of those paths scans batches or
-- batch_abnormalities.
--
-- Batch-level abnormal details are append-only: correcting a card's
-- inspection list never updates or deletes them.

CREATE TABLE IF NOT EXISTS items (
    id   INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cards (
    id                    INTEGER PRIMARY KEY,
    code                  TEXT NOT NULL UNIQUE,
    name                  TEXT NOT NULL,
    -- Number of received batches in the submitted state for this card.
    submitted_batch_count INTEGER NOT NULL DEFAULT 0
                          CHECK (submitted_batch_count >= 0)
);

-- The card's actual inspection list.  An item absent from this table is
-- "not inspected" for every received batch that uses this card.
CREATE TABLE IF NOT EXISTS card_items (
    card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES items(id),
    PRIMARY KEY (card_id, item_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS batches (
    id           INTEGER PRIMARY KEY,
    batch_no     TEXT NOT NULL UNIQUE,
    card_id      INTEGER NOT NULL REFERENCES cards(id),
    -- Only committed submissions are persisted.  The column records the
    -- single status that all supply aggregates are defined over.
    status       TEXT NOT NULL DEFAULT 'submitted'
                 CHECK (status IN ('submitted')),
    submitted_at TEXT NOT NULL
                 DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_batches_card
    ON batches(card_id, id);

-- Original abnormal detail rows, one per (batch, item).  Kept verbatim
-- across card-list corrections.
CREATE TABLE IF NOT EXISTS batch_abnormalities (
    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    item_id  INTEGER NOT NULL REFERENCES items(id),
    detail   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (batch_id, item_id)
) WITHOUT ROWID;

-- One row for every (card, item) pair, seeded with zeros.  Rows are never
-- added or removed by batch activity, so their count is fixed.
CREATE TABLE IF NOT EXISTS card_item_stats (
    card_id             INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    item_id             INTEGER NOT NULL REFERENCES items(id),
    abnormal_count      INTEGER NOT NULL DEFAULT 0 CHECK (abnormal_count >= 0),
    clean_count         INTEGER NOT NULL DEFAULT 0 CHECK (clean_count >= 0),
    not_inspected_count INTEGER NOT NULL DEFAULT 0 CHECK (not_inspected_count >= 0),
    PRIMARY KEY (card_id, item_id)
) WITHOUT ROWID;

-- Item query reads one entry per card, independent of received batches.
CREATE INDEX IF NOT EXISTS idx_card_item_stats_item
    ON card_item_stats(item_id, card_id);

-- Database-level backstops for the "no out-of-list abnormality" invariant.
-- The service validates first and rolls the whole operation back with a
-- friendly message; these triggers make the invariant hold even if a future
-- code path forgets the check.
CREATE TRIGGER IF NOT EXISTS trg_abnormality_requires_listed_item
BEFORE INSERT ON batch_abnormalities
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM batches b
    JOIN card_items ci
      ON ci.card_id = b.card_id AND ci.item_id = NEW.item_id
    WHERE b.id = NEW.batch_id AND b.status = 'submitted'
)
BEGIN
    SELECT RAISE(ABORT, 'abnormality item is not on the card inspection list');
END;

CREATE TRIGGER IF NOT EXISTS trg_list_cannot_drop_abnormal_item
BEFORE DELETE ON card_items
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM card_item_stats s
    WHERE s.card_id = OLD.card_id
      AND s.item_id = OLD.item_id
      AND s.abnormal_count > 0
)
BEGIN
    SELECT RAISE(ABORT, 'cannot remove an item that still has abnormal details');
END;
