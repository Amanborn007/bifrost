/** Publishes aggregated usage to ROLLUP_TOPIC for the billing pipeline. */

import { Rollup } from "./meter";

export interface TopicClient {
  publish(topic: string, message: string): Promise<void>;
}

export class RollupPublisher {
  constructor(
    private readonly client: TopicClient,
    private readonly rollupTopic: string,
  ) {}

  async publishRollups(rollups: Rollup[]): Promise<number> {
    for (const rollup of rollups) {
      await this.client.publish(this.rollupTopic, JSON.stringify(rollup));
    }
    return rollups.length;
  }
}
