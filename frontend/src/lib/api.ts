import type { Config, CredConfig, InstanceRecord } from './types';

const STORAGE_KEY = 'sparkle_cred_config';

export function saveCredConfig(config: CredConfig): void {
	sessionStorage.setItem(STORAGE_KEY, JSON.stringify(config));
}

export function loadCredConfig(): CredConfig {
	const raw = sessionStorage.getItem(STORAGE_KEY);
	if (!raw) return { source: 'local' };
	try {
		return JSON.parse(raw) as CredConfig;
	} catch {
		return { source: 'local' };
	}
}

export function clearCredConfig(): void {
	sessionStorage.removeItem(STORAGE_KEY);
}

function credHeaders(config: CredConfig): Record<string, string> {
	const h: Record<string, string> = { 'x-aws-cred-source': config.source };
	if (config.source === 'manual' && config.accessKeyId && config.secretAccessKey) {
		h['x-aws-access-key-id'] = config.accessKeyId;
		h['x-aws-secret-access-key'] = config.secretAccessKey;
		if (config.sessionToken) h['x-aws-session-token'] = config.sessionToken;
	}
	return h;
}

async function apiFetch<T>(path: string, config: CredConfig = loadCredConfig()): Promise<T> {
	const res = await fetch(path, { headers: credHeaders(config) });
	if (!res.ok) {
		const body = await res.json().catch(() => ({ detail: res.statusText }));
		throw new Error(body.detail ?? res.statusText);
	}
	return res.json() as Promise<T>;
}

export const listInstances = (region: string): Promise<InstanceRecord[]> =>
	apiFetch<InstanceRecord[]>(`/api/instances?region=${encodeURIComponent(region)}`);

export const listRegions = (): Promise<string[]> =>
	apiFetch<string[]>('/api/regions');

export const getConfig = (): Promise<Config> =>
	apiFetch<Config>('/api/config');
