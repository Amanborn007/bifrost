/** Subscribes to BILLING_TOPIC (published by the dispatcher) and feeds the meter. */

import { UsageMeter, UsageRecord } from "./meter";
import { RollupPublisher } from "./publisher";
import { Settings } from "./config";

export class BillingConsumer {
  constructor(
    private readonly settings: Settings,
    private readonly meter: UsageMeter,
    private readonly publisher: RollupPublisher,
  ) {}

  /** Handle one message from settings.billingTopic. */
  onMessage(raw: string): void {
    const record = JSON.parse(raw) as UsageRecord;
    this.meter.record(record);
  }

  /** Called every flushIntervalSeconds; sends the rollups downstream. */
  async flush(): Promise<number> {
    return this.publisher.publishRollups(this.meter.drain());
  }
}
