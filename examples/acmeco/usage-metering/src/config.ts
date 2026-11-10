/** Environment-driven settings; one config/<region>.env file per deployment. */

export interface Settings {
  region: string;
  billingTopic: string;
  rollupTopic: string;
  flushIntervalSeconds: number;
}

export function loadSettings(env: NodeJS.ProcessEnv = process.env): Settings {
  const required = (name: string): string => {
    const value = env[name];
    if (!value) {
      throw new Error(`missing required env var ${name}`);
    }
    return value;
  };
  return {
    region: required("REGION"),
    billingTopic: required("BILLING_TOPIC"),
    rollupTopic: required("ROLLUP_TOPIC"),
    flushIntervalSeconds: Number(env.FLUSH_INTERVAL_SECONDS ?? "300"),
  };
}
