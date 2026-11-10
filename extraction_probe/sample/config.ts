/**
 * Application configuration — TypeScript config for multi-language probe testing.
 */

export interface DatabaseConfig {
  host: string;
  port: number;
  database: string;
  maxConnections: number;
}

export interface AppConfig {
  serviceName: string;
  port: number;
  database: DatabaseConfig;
  enableMetrics: boolean;
}

export const defaultConfig: AppConfig = {
  serviceName: "order-service",
  port: 8080,
  database: {
    host: "localhost",
    port: 5432,
    database: "orders",
    maxConnections: 10,
  },
  enableMetrics: true,
};
