import { existsSync, mkdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { dirname, join } from "node:path";

export interface DataLocation {
  provider: string;
  localPath?: string;
  baseUrl?: string;
  dataRef?: string;
}

export interface RemoteOptions {
  cacheDir?: string;
  ttlSeconds?: number;
  timeoutSeconds?: number;
  autoRefresh?: boolean;
}

const DEFAULT_PAYPAL_URL = "https://raw.githubusercontent.com/smeinecke/paypal-fee-data/{data_ref}";
const DEFAULT_STRIPE_URL = "https://raw.githubusercontent.com/smeinecke/stripe-fee-data/{data_ref}";

function defaultCacheDir(): string {
  const homeCache = join(homedir(), ".cache");
  if (existsSync(homeCache)) {
    return join(homeCache, "payment-fee");
  }
  return join(tmpdir(), "payment-fee");
}

export class JsonDataSource {
  readonly location: DataLocation;
  readonly cacheDir: string;
  readonly ttlSeconds: number;
  readonly timeoutSeconds: number;
  readonly autoRefresh: boolean;

  constructor(location: DataLocation, options: RemoteOptions = {}) {
    this.location = location;
    this.cacheDir = options.cacheDir ?? defaultCacheDir();
    this.ttlSeconds = options.ttlSeconds ?? 24 * 60 * 60;
    this.timeoutSeconds = options.timeoutSeconds ?? 30;
    this.autoRefresh = options.autoRefresh ?? true;
  }

  async readText(relativePath: string): Promise<string> {
    if (this.location.localPath !== undefined) {
      return readFileSync(join(this.location.localPath, relativePath), "utf-8");
    }
    if (!this.location.baseUrl) {
      throw new Error(`No data source configured for ${this.location.provider}`);
    }
    return this.readRemote(relativePath);
  }

  async readJson<T = unknown>(relativePath: string): Promise<T> {
    const text = await this.readText(relativePath);
    return JSON.parse(text) as T;
  }

  /**
   * Force a refresh of ``relativePath`` by deleting any cached copy and
   * re-downloading it from the remote source.
   */
  async refresh(relativePath: string): Promise<string> {
    const cachePath = this.cachePath(relativePath);
    if (existsSync(cachePath)) {
      rmSync(cachePath, { force: true });
    }
    return this.readRemote(relativePath, { force: true });
  }

  private baseUrl(): string {
    const base = this.location.baseUrl ?? "";
    if (base.includes("{data_ref}")) {
      return base.replace("{data_ref}", this.location.dataRef ?? "main");
    }
    return base;
  }

  private cachePath(relativePath: string): string {
    const ref = this.location.dataRef ?? "default";
    const file = join(this.cacheDir, this.location.provider, ref, relativePath);
    mkdirSync(dirname(file), { recursive: true });
    return file;
  }

  private isFresh(cachePath: string): boolean {
    const stats = statSync(cachePath);
    return (Date.now() - stats.mtimeMs) / 1000 < this.ttlSeconds;
  }

  private async readRemote(
    relativePath: string,
    options: { force?: boolean } = {},
  ): Promise<string> {
    const cachePath = this.cachePath(relativePath);
    const hasCache = existsSync(cachePath);

    if (hasCache && (this.isFresh(cachePath) || (!options.force && !this.autoRefresh))) {
      return readFileSync(cachePath, "utf-8");
    }

    const url = `${this.baseUrl().replace(/\/$/, "")}/${relativePath.replace(/^\//, "")}`;
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), this.timeoutSeconds * 1000);
      const response = await fetch(url, {
        signal: controller.signal,
        redirect: "follow",
      });
      clearTimeout(timeout);
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }
      const text = await response.text();
      writeFileSync(cachePath, text, "utf-8");
      return text;
    } catch (error) {
      if (hasCache) {
        return readFileSync(cachePath, "utf-8");
      }
      throw new Error(
        `Failed to download ${url} for ${this.location.provider}: ${(error as Error).message}`,
      );
    }
  }
}

export function dataLocationFromString(
  provider: string,
  value: string,
  dataRef?: string,
): DataLocation {
  if (value.startsWith("http://") || value.startsWith("https://")) {
    return { provider, baseUrl: value, dataRef: dataRef ?? "main" };
  }
  return { provider, localPath: value, dataRef: dataRef ?? "local" };
}

export { DEFAULT_PAYPAL_URL, DEFAULT_STRIPE_URL };
