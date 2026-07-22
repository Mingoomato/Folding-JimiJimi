import path from 'node:path';
import fs from 'node:fs/promises';
import { safeStorage } from 'electron';
import { logError } from '@main/util/errors';
import { writeFileAtomic } from '@main/util/fsx';

export interface AuthStorage {
  getItem(key: string): Promise<string | null>;
  setItem(key: string, value: string): Promise<void>;
  removeItem(key: string): Promise<void>;
  clear(): Promise<void>;
}

/** cloud-api opaque session values encrypted with the operating-system keychain. */
export class SafeStorageAuthStorage implements AuthStorage {
  private readonly file: string;
  private values: Record<string, string> | null = null;

  constructor(userDataDir: string) {
    this.file = path.join(userDataDir, 'auth.bin');
  }

  async getItem(key: string): Promise<string | null> {
    const values = await this.readAll();
    return values[key] ?? null;
  }

  async setItem(key: string, value: string): Promise<void> {
    const values = await this.readAll();
    values[key] = value;
    await this.persist(values);
  }

  async removeItem(key: string): Promise<void> {
    const values = await this.readAll();
    delete values[key];
    await this.persist(values);
  }

  async clear(): Promise<void> {
    this.values = {};
    await fs.rm(this.file, { force: true });
  }

  private get encryptionAvailable(): boolean {
    try {
      return safeStorage.isEncryptionAvailable();
    } catch {
      return false;
    }
  }

  private async readAll(): Promise<Record<string, string>> {
    if (this.values) return this.values;
    if (!this.encryptionAvailable) {
      this.values = {};
      return this.values;
    }
    try {
      const blob = await fs.readFile(this.file);
      const parsed = JSON.parse(safeStorage.decryptString(blob)) as unknown;
      if (isStringRecord(parsed) && !isLegacyCredentials(parsed)) {
        this.values = parsed;
      } else {
        this.values = {};
        await fs.rm(this.file, { force: true });
      }
    } catch {
      this.values = {};
    }
    return this.values;
  }

  private async persist(values: Record<string, string>): Promise<void> {
    this.values = values;
    if (!this.encryptionAvailable) {
      logError('auth', new Error('safeStorage 사용 불가 — 인증 세션을 이번 실행 동안만 유지합니다.'));
      return;
    }
    await writeFileAtomic(this.file, safeStorage.encryptString(JSON.stringify(values)));
  }
}

export class MemoryAuthStorage implements AuthStorage {
  private readonly values = new Map<string, string>();

  async getItem(key: string): Promise<string | null> {
    return this.values.get(key) ?? null;
  }

  async setItem(key: string, value: string): Promise<void> {
    this.values.set(key, value);
  }

  async removeItem(key: string): Promise<void> {
    this.values.delete(key);
  }

  async clear(): Promise<void> {
    this.values.clear();
  }
}

function isStringRecord(value: unknown): value is Record<string, string> {
  return (
    typeof value === 'object' &&
    value !== null &&
    Object.values(value).every((item) => typeof item === 'string')
  );
}

function isLegacyCredentials(value: Record<string, string>): boolean {
  return typeof value.token === 'string' && typeof value.email === 'string';
}
