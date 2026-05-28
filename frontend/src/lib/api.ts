import type { Config, CostResource, CredConfig, EBSVolume, InstanceEvent, InstanceRecord, TagResource } from './types';

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

async function apiFetch<T>(path: string, config: CredConfig = loadCredConfig(), timeoutMs = 15000, init: RequestInit = {}): Promise<T> {
	const controller = new AbortController();
	const timer = setTimeout(() => controller.abort(), timeoutMs);
	try {
		const res = await fetch(path, {
			...init,
			headers: { ...credHeaders(config), ...(init.headers as Record<string, string> ?? {}) },
			signal: controller.signal,
		});
		if (!res.ok) {
			const body = await res.json().catch(() => ({ detail: res.statusText }));
			throw new Error(body.detail ?? res.statusText);
		}
		if (res.status === 204) return undefined as T;
		return res.json() as Promise<T>;
	} catch (e) {
		if (e instanceof DOMException && e.name === 'AbortError') throw new Error('Request timed out');
		throw e;
	} finally {
		clearTimeout(timer);
	}
}

export const listInstances = (region: string): Promise<InstanceRecord[]> =>
	apiFetch<InstanceRecord[]>(`/api/instances?region=${encodeURIComponent(region)}`, loadCredConfig(), 30000);

export const listRegions = (hintRegion?: string): Promise<string[]> =>
	apiFetch<string[]>(hintRegion ? `/api/regions?region=${encodeURIComponent(hintRegion)}` : '/api/regions', loadCredConfig(), 30000);

export const listEvents = (region: string): Promise<InstanceEvent[]> =>
	apiFetch<InstanceEvent[]>(`/api/events?region=${encodeURIComponent(region)}`, loadCredConfig(), 120000);

export const getConfig = (): Promise<Config> =>
	apiFetch<Config>('/api/config');

function instanceAction(method: string, instanceId: string, region: string, body?: unknown): Promise<{ previous_state: string; current_state: string }> {
	return apiFetch<{ previous_state: string; current_state: string }>(
		`/api/instances/${encodeURIComponent(instanceId)}/${method}?region=${encodeURIComponent(region)}`,
		loadCredConfig(),
		30000,
		{ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined },
	);
}

export const startInstance = (region: string, instanceId: string) => instanceAction('start', instanceId, region);
export const stopInstance = (region: string, instanceId: string) => instanceAction('stop', instanceId, region);
export const terminateInstance = (region: string, instanceId: string) => instanceAction('terminate', instanceId, region);
export const rebootInstance = (region: string, instanceId: string) => instanceAction('reboot', instanceId, region);

export const updateTags = (
	region: string,
	instanceId: string,
	upsert: { Key: string; Value: string }[],
	deleteKeys: string[],
): Promise<void> =>
	apiFetch<void>(
		`/api/instances/${encodeURIComponent(instanceId)}/tags?region=${encodeURIComponent(region)}`,
		loadCredConfig(),
		30000,
		{ method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ upsert, delete_keys: deleteKeys }) },
	);

export const searchByTag = (region: string, key: string, value?: string): Promise<TagResource[]> => {
	const qs = new URLSearchParams({ region, key });
	if (value) qs.set('value', value);
	return apiFetch<TagResource[]>(`/api/tag-search?${qs}`, loadCredConfig(), 30000);
};

export const listVolumes = (region: string): Promise<EBSVolume[]> =>
	apiFetch<EBSVolume[]>(`/api/volumes?region=${encodeURIComponent(region)}`, loadCredConfig(), 30000);

export const listCostResources = (region: string): Promise<CostResource[]> =>
	apiFetch<CostResource[]>(`/api/cost-resources?region=${encodeURIComponent(region)}`, loadCredConfig(), 60000);

export interface S3QueryParams {
	bucket: string;
	bucketRegion?: string;
	prefix?: string;
	startDate?: string;
	endDate?: string;
}

export const fetchS3Events = (region: string, params: S3QueryParams): Promise<InstanceEvent[]> => {
	const qs = new URLSearchParams({ bucket: params.bucket, region });
	if (params.bucketRegion) qs.set('bucket_region', params.bucketRegion);
	if (params.prefix) qs.set('prefix', params.prefix);
	if (params.startDate) qs.set('start_date', params.startDate);
	if (params.endDate) qs.set('end_date', params.endDate);
	return apiFetch<InstanceEvent[]>(`/api/s3-events?${qs}`, loadCredConfig(), 300000); // 5 min timeout
};
