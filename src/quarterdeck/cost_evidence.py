"""Cost evidence beside the old turn layout, invalidated by an older writer's edits."""


from typing import NamedTuple


class CostReading(NamedTuple):
    recorded_cost: float | None
    cost_basis: str | None
    valid: int


class CostDetails(NamedTuple):
    unpriced_steps: int
    steps: str | None


class CostEvidence:
    def __init__(self, db, create=True):
        self.db = db
        if not create:
            return
        db.execute('''CREATE TABLE IF NOT EXISTS turn_costs (
            record TEXT NOT NULL, turn INTEGER NOT NULL, recorded_cost REAL, cost_basis TEXT,
            valid INTEGER NOT NULL, unpriced_steps INTEGER NOT NULL, steps TEXT, PRIMARY KEY(record,turn))''')
        for event in ('INSERT', 'UPDATE'):
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS cost_after_{event.lower()} AFTER {event} ON turns
                BEGIN UPDATE turn_costs SET valid=0 WHERE record=NEW.record AND turn=NEW.turn; END''')

    def present(self):
        return self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='turn_costs'").fetchone() is not None

    def get(self, record, index) -> CostReading | None:
        row = self.db.execute("SELECT recorded_cost,cost_basis,valid FROM turn_costs WHERE record=? AND turn=?",
                              (str(record), index)).fetchone()
        return CostReading(*row) if row is not None else None

    def details(self, record, index) -> CostDetails | None:
        row = self.db.execute("SELECT unpriced_steps,steps FROM turn_costs WHERE record=? AND turn=?",
                              (str(record), index)).fetchone()
        return CostDetails(*row) if row is not None else None

    def put(self, record, index, recorded, basis, unpriced=0, steps=None):
        self.db.execute("INSERT OR REPLACE INTO turn_costs (record,turn,recorded_cost,cost_basis,valid,unpriced_steps,steps) "
                        "VALUES (?,?,?,?,1,?,?)",
                        (str(record), index, recorded, basis, unpriced, steps))

    def known(self):
        if not self.present():
            return 'cost > 0'  # an old zero has no evidence proving a genuine zero
        same = 'c.record=turns.record AND c.turn=turns.turn'
        return f'''cost IS NOT NULL AND (
            EXISTS (SELECT 1 FROM turn_costs c WHERE {same} AND c.valid=1 AND c.cost_basis IS NOT NULL)
            OR (cost > 0 AND NOT EXISTS (SELECT 1 FROM turn_costs c WHERE {same})))'''

    def complete(self):
        if not self.present():
            return self.known()
        return f'''({self.known()}) AND NOT EXISTS (SELECT 1 FROM turn_costs c
            WHERE c.record=turns.record AND c.turn=turns.turn AND c.unpriced_steps>0)'''
