/** Aggregates per-delivery usage records into per-account counters. */

export interface UsageRecord {
  account_id: string;
  event_id: string;
  subscription_id: string;
  delivered: boolean;
}

export interface Rollup {
  account_id: string;
  attempted: number;
  delivered: number;
}

export class UsageMeter {
  private counters = new Map<string, Rollup>();

  record(rec: UsageRecord): void {
    const current = this.counters.get(rec.account_id) ?? {
      account_id: rec.account_id,
      attempted: 0,
      delivered: 0,
    };
    current.attempted += 1;
    if (rec.delivered) {
      current.delivered += 1;
    }
    this.counters.set(rec.account_id, current);
  }

  drain(): Rollup[] {
    const rollups = [...this.counters.values()];
    this.counters.clear();
    return rollups;
  }
}
