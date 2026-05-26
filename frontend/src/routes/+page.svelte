<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { getConfig, listEvents, listInstances, listRegions, loadCredConfig } from '$lib/api';
	import type { InstanceEvent, InstanceRecord } from '$lib/types';

	let region = $state('us-east-1');
	let regions = $state<string[]>([]);
	let instances = $state<InstanceRecord[]>([]);
	let loading = $state(true);
	let refreshing = $state(false);
	let error = $state<string | null>(null);
	let autoRefresh = $state(true);
	let lastRefreshed = $state<Date | null>(null);
	let autoRefreshTimer: ReturnType<typeof setInterval> | null = null;

	let activeTab = $state<'instances' | 'timeline'>('instances');
	let events = $state<InstanceEvent[]>([]);
	let eventsLoading = $state(false);
	let eventsError = $state<string | null>(null);
	let eventsLoaded = $state(false);

	// File import
	let fileInputEl = $state<HTMLInputElement | null>(null);
	let importedEvents = $state<InstanceEvent[]>([]);
	let importFileName = $state<string | null>(null);
	let importError = $state<string | null>(null);
	let importSource = $state<'api' | 'file'>('api');

	const TARGET_EVENTS = new Set(['RunInstances', 'StartInstances', 'StopInstances', 'TerminateInstances']);

	const STATE_COLORS: Record<string, string> = {
		running: '#22c55e',
		stopped: '#eab308',
		terminated: '#ef4444',
		pending: '#a78bfa',
		stopping: '#f97316',
		shutting_down: '#f97316'
	};

	const EVENT_LABELS: Record<string, string> = {
		RunInstances: 'Launched',
		StartInstances: 'Started',
		StopInstances: 'Stopped',
		TerminateInstances: 'Terminated',
	};

	const EVENT_COLORS: Record<string, string> = {
		RunInstances: '#6366f1',
		StartInstances: '#22c55e',
		StopInstances: '#eab308',
		TerminateInstances: '#ef4444',
	};

	function stateColor(s: string): string { return STATE_COLORS[s] ?? '#71717a'; }
	function eventColor(n: string): string { return EVENT_COLORS[n] ?? '#71717a'; }

	function fmtDate(iso: string | null): string {
		if (!iso) return '';
		return new Date(iso).toLocaleString();
	}

	function relativeTime(iso: string | null): string {
		if (!iso) return '';
		const diff = Date.now() - new Date(iso).getTime();
		const d = Math.floor(diff / 86400000);
		const h = Math.floor((diff % 86400000) / 3600000);
		if (d > 0) return `${d}d ${h}h ago`;
		const m = Math.floor((diff % 3600000) / 60000);
		if (h > 0) return `${h}h ${m}m ago`;
		return `${m}m ago`;
	}

	const instanceNameMap = $derived(
		Object.fromEntries(instances.map(i => [i.instance_id, i.name]))
	);

	// Active event list — file import overrides live API data
	const activeEvents = $derived(importSource === 'file' ? importedEvents : events);

	const groupedEvents = $derived(
		Object.entries(
			activeEvents.reduce((acc: Record<string, InstanceEvent[]>, e) => {
				(acc[e.instance_id] ??= []).push(e);
				return acc;
			}, {})
		).sort(([a], [b]) =>
			(instanceNameMap[a] ?? a).localeCompare(instanceNameMap[b] ?? b)
		)
	);

	// ── CloudTrail file parsing ──────────────────────────────────────────────

	function parseCloudTrailJson(raw: string): InstanceEvent[] {
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		let parsed: any;
		try {
			parsed = JSON.parse(raw);
		} catch {
			throw new Error('File is not valid JSON');
		}

		// Our own grouped export: [{ instance_id, instance_name, events: [...] }]
		if (Array.isArray(parsed) && parsed[0]?.events) {
			// eslint-disable-next-line @typescript-eslint/no-explicit-any
			return (parsed as any[]).flatMap((g: any) => g.events as InstanceEvent[])
				.sort((a, b) => a.event_time.localeCompare(b.event_time));
		}

		// Raw CloudTrail S3 format: { Records: [...] }
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		const records: any[] = parsed?.Records ?? (Array.isArray(parsed) ? parsed : null);
		if (!records) throw new Error('Unrecognised format — expected { Records: [...] } or Sparkle JSON export');

		const result: InstanceEvent[] = [];
		for (const rec of records) {
			const eventName: string = rec.eventName ?? '';
			if (!TARGET_EVENTS.has(eventName)) continue;

			const eventTime: string = rec.eventTime ?? '';
			// eslint-disable-next-line @typescript-eslint/no-explicit-any
			const identity: any = rec.userIdentity ?? {};
			const username: string | null = identity.userName ?? identity.arn ?? identity.type ?? null;
			const sourceIp: string | null = rec.sourceIPAddress ?? null;

			// eslint-disable-next-line @typescript-eslint/no-explicit-any
			let items: any[] = [];
			if (eventName === 'RunInstances') {
				items = rec.responseElements?.instancesSet?.items ?? [];
			} else {
				items = rec.requestParameters?.instancesSet?.items ?? [];
			}

			for (const item of items) {
				const iid: string = item.instanceId ?? '';
				if (iid) result.push({ event_time: eventTime, event_name: eventName, instance_id: iid, username, source_ip: sourceIp });
			}
		}

		if (result.length === 0) throw new Error('No RunInstances/StartInstances/StopInstances/TerminateInstances events found in file');
		return result.sort((a, b) => a.event_time.localeCompare(b.event_time));
	}

	function handleFileChange(e: Event) {
		const file = (e.target as HTMLInputElement).files?.[0];
		if (!file) return;
		importError = null;
		const reader = new FileReader();
		reader.onload = () => {
			try {
				const parsed = parseCloudTrailJson(reader.result as string);
				importedEvents = parsed;
				importFileName = file.name;
				importSource = 'file';
				activeTab = 'timeline';
			} catch (err) {
				importError = err instanceof Error ? err.message : String(err);
				importFileName = null;
			}
			// Reset input so the same file can be re-selected
			if (fileInputEl) fileInputEl.value = '';
		};
		reader.readAsText(file);
	}

	function clearImport() {
		importedEvents = [];
		importFileName = null;
		importError = null;
		importSource = 'api';
	}

	// ── Live data ────────────────────────────────────────────────────────────

	async function loadInstances(isBackground = false) {
		if (!isBackground) {
			loading = instances.length === 0;
			refreshing = instances.length > 0;
		} else {
			refreshing = true;
		}
		error = null;
		try {
			instances = await listInstances(region);
			lastRefreshed = new Date();
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
			refreshing = false;
		}
	}

	async function loadEventsData() {
		eventsLoading = true;
		eventsError = null;
		try {
			events = await listEvents(region);
			eventsLoaded = true;
		} catch (e) {
			eventsError = e instanceof Error ? e.message : String(e);
		} finally {
			eventsLoading = false;
		}
	}

	function startAutoRefresh() {
		stopAutoRefresh();
		if (autoRefresh) autoRefreshTimer = setInterval(() => loadInstances(true), 30000);
	}

	function stopAutoRefresh() {
		if (autoRefreshTimer !== null) { clearInterval(autoRefreshTimer); autoRefreshTimer = null; }
	}

	async function onRegionChange() {
		instances = [];
		events = [];
		eventsLoaded = false;
		await loadInstances();
		if (activeTab === 'timeline' && importSource === 'api') await loadEventsData();
		startAutoRefresh();
	}

	function toggleAutoRefresh() {
		autoRefresh = !autoRefresh;
		autoRefresh ? startAutoRefresh() : stopAutoRefresh();
	}

	async function switchTab(tab: 'instances' | 'timeline') {
		activeTab = tab;
		if (tab === 'timeline' && importSource === 'api' && !eventsLoaded && !eventsLoading) {
			await loadEventsData();
		}
	}

	function onCredentialsSaved() {
		const stored = loadCredConfig();
		if (stored.region) {
			region = stored.region;
			if (!regions.includes(region)) regions = [region, ...regions];
		}
		instances = [];
		events = [];
		eventsLoaded = false;
		loadInstances();
		if (activeTab === 'timeline' && importSource === 'api') loadEventsData();
		startAutoRefresh();
	}

	// ── Downloads ────────────────────────────────────────────────────────────

	function downloadJSON() {
		const payload = groupedEvents.map(([id, evts]) => ({
			instance_id: id,
			instance_name: instanceNameMap[id] ?? id,
			events: evts,
		}));
		triggerDownload(
			new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' }),
			`sparkle-timeline-${region}-${today()}.json`
		);
	}

	function downloadCSV() {
		const rows: string[][] = [['event_time', 'event_name', 'instance_id', 'instance_name', 'username', 'source_ip']];
		for (const e of activeEvents) {
			rows.push([e.event_time, e.event_name, e.instance_id, instanceNameMap[e.instance_id] ?? e.instance_id, e.username ?? '', e.source_ip ?? '']);
		}
		const csv = rows.map(r => r.map(c => `"${c.replace(/"/g, '""')}"`).join(',')).join('\n');
		triggerDownload(new Blob([csv], { type: 'text/csv' }), `sparkle-timeline-${region}-${today()}.csv`);
	}

	function triggerDownload(blob: Blob, filename: string) {
		const url = URL.createObjectURL(blob);
		const a = document.createElement('a');
		a.href = url; a.download = filename; a.click();
		URL.revokeObjectURL(url);
	}

	function today(): string { return new Date().toISOString().split('T')[0]; }

	// ── Lifecycle ────────────────────────────────────────────────────────────

	onMount(async () => {
		try {
			const [cfg, regs] = await Promise.all([getConfig(), listRegions()]);
			const stored = loadCredConfig();
			region = stored.region || cfg.default_region;
			regions = regs.includes(region) ? regs : [region, ...regs];
		} catch {
			const stored = loadCredConfig();
			region = stored.region || 'us-east-1';
			regions = [region];
		}
		await loadInstances();
		startAutoRefresh();
		window.addEventListener('sparkle:credentials-saved', onCredentialsSaved);
	});

	onDestroy(() => {
		stopAutoRefresh();
		window.removeEventListener('sparkle:credentials-saved', onCredentialsSaved);
	});
</script>

<!-- Hidden file input -->
<input
	type="file"
	accept=".json"
	bind:this={fileInputEl}
	onchange={handleFileChange}
	class="hidden"
/>

<!-- Toolbar -->
<div class="flex flex-wrap items-center gap-3 mb-0 pb-0">
	<div class="flex items-center gap-2">
		<label for="region-select" class="text-sm font-medium" style="color: var(--color-muted);">Region</label>
		<select
			id="region-select"
			bind:value={region}
			onchange={onRegionChange}
			class="rounded px-2 py-1 text-sm border"
			style="background-color: var(--color-surface); border-color: var(--color-border); color: var(--color-text);"
		>
			{#each regions as r}
				<option value={r}>{r}</option>
			{/each}
		</select>
	</div>

	{#if activeTab === 'instances'}
		<button
			onclick={() => loadInstances()}
			disabled={loading || refreshing}
			class="flex items-center gap-1.5 rounded px-3 py-1.5 text-sm font-medium transition-colors disabled:opacity-50"
			style="background-color: var(--color-accent); color: white;"
		>
			{#if refreshing}
				<svg class="w-3.5 h-3.5 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 11-6.219-8.56"/></svg>
			{/if}
			Refresh
		</button>

		<label class="flex items-center gap-2 text-sm cursor-pointer select-none">
			<input type="checkbox" checked={autoRefresh} onchange={toggleAutoRefresh} class="rounded" />
			<span style="color: var(--color-muted);">Auto-refresh (30s)</span>
		</label>

		<span class="text-sm ml-auto" style="color: var(--color-muted);">
			{#if !loading}
				{instances.length} instance{instances.length !== 1 ? 's' : ''}
				{#if lastRefreshed}· refreshed {relativeTime(lastRefreshed.toISOString())}{/if}
			{/if}
		</span>
	{:else}
		<!-- Load CloudTrail file button -->
		<button
			onclick={() => fileInputEl?.click()}
			class="flex items-center gap-1.5 rounded px-3 py-1.5 text-sm font-medium border transition-colors"
			style="border-color: var(--color-accent); color: var(--color-accent);"
			title="Load a CloudTrail JSON log file from your machine"
		>
			<svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
				<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>
			</svg>
			Load CloudTrail Logs
		</button>

		{#if importSource === 'file'}
			<!-- Imported file badge -->
			<span class="flex items-center gap-1.5 rounded px-2.5 py-1.5 text-xs font-medium" style="background-color: #6366f122; color: #a5b4fc;">
				<svg class="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
				{importFileName}
			</span>
			<button
				onclick={clearImport}
				class="text-xs rounded px-2 py-1 border transition-colors"
				style="border-color: var(--color-border); color: var(--color-muted);"
				title="Clear imported file and switch back to live API data"
			>
				Clear
			</button>
		{:else}
			<button
				onclick={loadEventsData}
				disabled={eventsLoading}
				class="flex items-center gap-1.5 rounded px-3 py-1.5 text-sm font-medium transition-colors disabled:opacity-50"
				style="background-color: var(--color-accent); color: white;"
			>
				{#if eventsLoading}
					<svg class="w-3.5 h-3.5 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 11-6.219-8.56"/></svg>
				{/if}
				Refresh
			</button>
		{/if}

		{#if activeEvents.length > 0}
			<button
				onclick={downloadJSON}
				class="flex items-center gap-1.5 rounded px-3 py-1.5 text-sm font-medium border transition-colors"
				style="border-color: var(--color-border); color: var(--color-text);"
			>
				<svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
					<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>
				</svg>
				JSON
			</button>
			<button
				onclick={downloadCSV}
				class="flex items-center gap-1.5 rounded px-3 py-1.5 text-sm font-medium border transition-colors"
				style="border-color: var(--color-border); color: var(--color-text);"
			>
				<svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
					<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>
				</svg>
				CSV
			</button>
		{/if}

		<span class="text-sm ml-auto" style="color: var(--color-muted);">
			{#if activeEvents.length > 0}
				{activeEvents.length} event{activeEvents.length !== 1 ? 's' : ''} across {groupedEvents.length} instance{groupedEvents.length !== 1 ? 's' : ''}
				{#if importSource === 'file'}<span class="ml-1" style="color: #a5b4fc;">· from file</span>{/if}
			{/if}
		</span>
	{/if}
</div>

<!-- Tab bar -->
<div class="flex gap-0 mt-4 mb-5 border-b" style="border-color: var(--color-border);">
	<button
		onclick={() => switchTab('instances')}
		class="px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors"
		style="border-color: {activeTab === 'instances' ? 'var(--color-accent)' : 'transparent'}; color: {activeTab === 'instances' ? 'var(--color-text)' : 'var(--color-muted)'};"
	>
		Instances
	</button>
	<button
		onclick={() => switchTab('timeline')}
		class="px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors"
		style="border-color: {activeTab === 'timeline' ? 'var(--color-accent)' : 'transparent'}; color: {activeTab === 'timeline' ? 'var(--color-text)' : 'var(--color-muted)'};"
	>
		Timeline{#if importSource === 'file'} <span class="ml-1 w-1.5 h-1.5 rounded-full inline-block" style="background-color: #6366f1; vertical-align: middle;"></span>{/if}
	</button>
</div>

<!-- ── INSTANCES TAB ── -->
{#if activeTab === 'instances'}
	{#if error}
		<div class="mb-4 rounded border px-4 py-3 text-sm" style="background-color: #1f0a0a; border-color: #7f1d1d; color: #fca5a5;">
			<strong>AWS Error:</strong> {error}
		</div>
	{/if}

	<div class="rounded-lg border overflow-hidden" style="border-color: var(--color-border);">
		<div class="overflow-x-auto">
			<table class="w-full text-sm">
				<thead>
					<tr style="background-color: var(--color-surface); border-bottom: 1px solid var(--color-border);">
						<th class="text-left px-4 py-3 font-semibold" style="color: var(--color-muted);">Name</th>
						<th class="text-left px-4 py-3 font-semibold" style="color: var(--color-muted);">Instance ID</th>
						<th class="text-left px-4 py-3 font-semibold" style="color: var(--color-muted);">State</th>
						<th class="text-left px-4 py-3 font-semibold" style="color: var(--color-muted);">Type</th>
						<th class="text-left px-4 py-3 font-semibold" style="color: var(--color-muted);">AZ</th>
						<th class="text-left px-4 py-3 font-semibold" style="color: var(--color-muted);">Launch Time</th>
						<th class="text-left px-4 py-3 font-semibold" style="color: var(--color-muted);">First Started</th>
						<th class="text-left px-4 py-3 font-semibold" style="color: var(--color-muted);">Username</th>
					</tr>
				</thead>
				<tbody>
					{#if loading}
						{#each { length: 5 } as _}
							<tr style="border-bottom: 1px solid var(--color-border);">
								{#each { length: 8 } as _}
									<td class="px-4 py-3"><div class="h-4 rounded animate-pulse w-24" style="background-color: var(--color-border);"></div></td>
								{/each}
							</tr>
						{/each}
					{:else if instances.length === 0 && !error}
						<tr>
							<td colspan="8" class="px-4 py-12 text-center" style="color: var(--color-muted);">No instances found in {region}</td>
						</tr>
					{:else}
						{#each instances as inst (inst.instance_id)}
							<tr
								class="transition-colors"
								style="border-bottom: 1px solid var(--color-border);"
								onmouseenter={(e) => (e.currentTarget as HTMLElement).style.backgroundColor = 'var(--color-surface)'}
								onmouseleave={(e) => (e.currentTarget as HTMLElement).style.backgroundColor = ''}
							>
								<td class="px-4 py-3 font-medium">{inst.name}</td>
								<td class="px-4 py-3 font-mono text-xs" style="color: var(--color-muted);">{inst.instance_id}</td>
								<td class="px-4 py-3">
									<span class="inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium"
										style="background-color: {stateColor(inst.state)}22; color: {stateColor(inst.state)};">
										<span class="w-1.5 h-1.5 rounded-full" style="background-color: {stateColor(inst.state)};"></span>
										{inst.state}
									</span>
								</td>
								<td class="px-4 py-3" style="color: var(--color-muted);">{inst.instance_type}</td>
								<td class="px-4 py-3" style="color: var(--color-muted);">{inst.availability_zone}</td>
								<td class="px-4 py-3 whitespace-nowrap" title={inst.launch_time}>{fmtDate(inst.launch_time)}</td>
								<td class="px-4 py-3 whitespace-nowrap">
									{#if inst.first_started}
										<span title={inst.first_started}>{fmtDate(inst.first_started)}</span>
									{:else}
										<span title="No CloudTrail RunInstances event found — instance may be older than 90 days or CloudTrail access is restricted" style="color: var(--color-muted);" class="cursor-help">N/A</span>
									{/if}
								</td>
								<td class="px-4 py-3">
									{#if inst.username}
										<span class="font-mono text-xs">{inst.username}</span>
									{:else}
										<span title="No CloudTrail RunInstances event found — instance may be older than 90 days or CloudTrail access is restricted" style="color: var(--color-muted);" class="cursor-help">N/A</span>
									{/if}
								</td>
							</tr>
						{/each}
					{/if}
				</tbody>
			</table>
		</div>
	</div>

<!-- ── TIMELINE TAB ── -->
{:else}
	{#if importError}
		<div class="mb-4 rounded border px-4 py-3 text-sm" style="background-color: #1f0a0a; border-color: #7f1d1d; color: #fca5a5;">
			<strong>Import error:</strong> {importError}
		</div>
	{/if}

	{#if eventsError && importSource === 'api'}
		<div class="mb-4 rounded border px-4 py-3 text-sm" style="background-color: #1f0a0a; border-color: #7f1d1d; color: #fca5a5;">
			<strong>AWS Error:</strong> {eventsError}
		</div>
	{/if}

	{#if eventsLoading}
		<div class="flex flex-col gap-4">
			{#each { length: 3 } as _}
				<div class="rounded-lg border overflow-hidden" style="border-color: var(--color-border);">
					<div class="px-4 py-3 border-b flex gap-3" style="background-color: var(--color-surface); border-color: var(--color-border);">
						<div class="h-4 w-48 rounded animate-pulse" style="background-color: var(--color-border);"></div>
						<div class="h-4 w-32 rounded animate-pulse" style="background-color: var(--color-border);"></div>
					</div>
					{#each { length: 2 } as _}
						<div class="px-4 py-3 flex gap-6 border-b" style="border-color: var(--color-border);">
							{#each { length: 4 } as _}
								<div class="h-4 w-28 rounded animate-pulse" style="background-color: var(--color-border);"></div>
							{/each}
						</div>
					{/each}
				</div>
			{/each}
		</div>
	{:else if groupedEvents.length === 0 && !eventsError && !importError}
		<div class="rounded-lg border p-10 text-center" style="border-color: var(--color-border); border-style: dashed;">
			<svg class="w-8 h-8 mx-auto mb-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" style="color: var(--color-muted);">
				<path d="M9 12h6m-6 4h6m2 5H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5.586a1 1 0 0 1 .707.293l5.414 5.414a1 1 0 0 1 .293.707V19a2 2 0 0 1-2 2z"/>
			</svg>
			<p class="text-sm font-medium mb-1">No event history loaded</p>
			<p class="text-xs mb-4" style="color: var(--color-muted);">
				Load a CloudTrail JSON log file from your machine, or use live AWS credentials to fetch directly.
			</p>
			<button
				onclick={() => fileInputEl?.click()}
				class="inline-flex items-center gap-2 rounded px-4 py-2 text-sm font-medium"
				style="background-color: var(--color-accent); color: white;"
			>
				<svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
					<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>
				</svg>
				Load CloudTrail Logs
			</button>
		</div>
	{:else}
		<div class="flex flex-col gap-4">
			{#each groupedEvents as [instanceId, instanceEvents]}
				<div class="rounded-lg border overflow-hidden" style="border-color: var(--color-border);">
					<!-- Instance header -->
					<div class="px-4 py-3 flex items-center gap-3 border-b" style="background-color: var(--color-surface); border-color: var(--color-border);">
						<span class="font-semibold text-sm">{instanceNameMap[instanceId] ?? instanceId}</span>
						{#if instanceNameMap[instanceId] && instanceNameMap[instanceId] !== instanceId}
							<span class="font-mono text-xs" style="color: var(--color-muted);">{instanceId}</span>
						{/if}
						{#each instances.filter(i => i.instance_id === instanceId) as inst}
							<span class="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium"
								style="background-color: {stateColor(inst.state)}22; color: {stateColor(inst.state)};">
								<span class="w-1.5 h-1.5 rounded-full" style="background-color: {stateColor(inst.state)};"></span>
								{inst.state}
							</span>
							<span class="text-xs" style="color: var(--color-muted);">{inst.instance_type} · {inst.availability_zone}</span>
						{/each}
						<span class="ml-auto text-xs" style="color: var(--color-muted);">
							{instanceEvents.length} event{instanceEvents.length !== 1 ? 's' : ''}
						</span>
					</div>
					<!-- Events table -->
					<table class="w-full text-sm">
						<thead>
							<tr style="border-bottom: 1px solid var(--color-border);">
								<th class="text-left px-4 py-2 font-semibold text-xs" style="color: var(--color-muted);">Time</th>
								<th class="text-left px-4 py-2 font-semibold text-xs" style="color: var(--color-muted);">Event</th>
								<th class="text-left px-4 py-2 font-semibold text-xs" style="color: var(--color-muted);">User</th>
								<th class="text-left px-4 py-2 font-semibold text-xs" style="color: var(--color-muted);">Source IP</th>
							</tr>
						</thead>
						<tbody>
							{#each instanceEvents as evt (evt.event_time + evt.event_name + evt.instance_id)}
								<tr
									style="border-bottom: 1px solid var(--color-border);"
									onmouseenter={(e) => (e.currentTarget as HTMLElement).style.backgroundColor = 'var(--color-surface)'}
									onmouseleave={(e) => (e.currentTarget as HTMLElement).style.backgroundColor = ''}
								>
									<td class="px-4 py-2.5 whitespace-nowrap text-xs" title={evt.event_time}>{fmtDate(evt.event_time)}</td>
									<td class="px-4 py-2.5">
										<span class="inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium"
											style="background-color: {eventColor(evt.event_name)}22; color: {eventColor(evt.event_name)};">
											<span class="w-1.5 h-1.5 rounded-full" style="background-color: {eventColor(evt.event_name)};"></span>
											{EVENT_LABELS[evt.event_name] ?? evt.event_name}
										</span>
									</td>
									<td class="px-4 py-2.5 font-mono text-xs" style="color: var(--color-muted);">{evt.username ?? '—'}</td>
									<td class="px-4 py-2.5 font-mono text-xs" style="color: var(--color-muted);">{evt.source_ip ?? '—'}</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{/each}
		</div>
	{/if}
{/if}
