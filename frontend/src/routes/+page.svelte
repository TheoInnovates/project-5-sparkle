<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { getConfig, listInstances, listRegions } from '$lib/api';
	import type { InstanceRecord } from '$lib/types';

	let region = $state('us-east-1');
	let regions = $state<string[]>([]);
	let instances = $state<InstanceRecord[]>([]);
	let loading = $state(true);
	let refreshing = $state(false);
	let error = $state<string | null>(null);
	let autoRefresh = $state(true);
	let lastRefreshed = $state<Date | null>(null);
	let autoRefreshTimer: ReturnType<typeof setInterval> | null = null;

	const STATE_COLORS: Record<string, string> = {
		running: '#22c55e',
		stopped: '#eab308',
		terminated: '#ef4444',
		pending: '#a78bfa',
		stopping: '#f97316',
		shutting_down: '#f97316'
	};

	function stateColor(state: string): string {
		return STATE_COLORS[state] ?? '#71717a';
	}

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

	function startAutoRefresh() {
		stopAutoRefresh();
		if (autoRefresh) {
			autoRefreshTimer = setInterval(() => loadInstances(true), 30000);
		}
	}

	function stopAutoRefresh() {
		if (autoRefreshTimer !== null) {
			clearInterval(autoRefreshTimer);
			autoRefreshTimer = null;
		}
	}

	async function onRegionChange() {
		instances = [];
		await loadInstances();
		startAutoRefresh();
	}

	function toggleAutoRefresh() {
		autoRefresh = !autoRefresh;
		if (autoRefresh) {
			startAutoRefresh();
		} else {
			stopAutoRefresh();
		}
	}

	function onCredentialsSaved() {
		instances = [];
		loadInstances();
		startAutoRefresh();
	}

	onMount(async () => {
		try {
			const [cfg, regs] = await Promise.all([getConfig(), listRegions()]);
			region = cfg.default_region;
			regions = regs;
		} catch {
			regions = ['us-east-1'];
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

<!-- Toolbar -->
<div class="flex flex-wrap items-center gap-3 mb-5">
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

	<button
		onclick={() => loadInstances()}
		disabled={loading || refreshing}
		class="flex items-center gap-1.5 rounded px-3 py-1.5 text-sm font-medium transition-colors disabled:opacity-50"
		style="background-color: var(--color-accent); color: white;"
	>
		{#if refreshing}
			<svg class="w-3.5 h-3.5 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
				<path d="M21 12a9 9 0 11-6.219-8.56"/>
			</svg>
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
			{#if lastRefreshed}
				· refreshed {relativeTime(lastRefreshed.toISOString())}
			{/if}
		{/if}
	</span>
</div>

<!-- Error banner -->
{#if error}
	<div class="mb-4 rounded border px-4 py-3 text-sm" style="background-color: #1f0a0a; border-color: #7f1d1d; color: #fca5a5;">
		<strong>AWS Error:</strong> {error}
	</div>
{/if}

<!-- Table -->
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
								<td class="px-4 py-3">
									<div class="h-4 rounded animate-pulse w-24" style="background-color: var(--color-border);"></div>
								</td>
							{/each}
						</tr>
					{/each}
				{:else if instances.length === 0 && !error}
					<tr>
						<td colspan="8" class="px-4 py-12 text-center" style="color: var(--color-muted);">
							No instances found in {region}
						</td>
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
							<td class="px-4 py-3 font-mono text-xs" style="color: var(--color-muted);">
								{inst.instance_id}
							</td>
							<td class="px-4 py-3">
								<span
									class="inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium"
									style="background-color: {stateColor(inst.state)}22; color: {stateColor(inst.state)};"
								>
									<span class="w-1.5 h-1.5 rounded-full" style="background-color: {stateColor(inst.state)};"></span>
									{inst.state}
								</span>
							</td>
							<td class="px-4 py-3" style="color: var(--color-muted);">{inst.instance_type}</td>
							<td class="px-4 py-3" style="color: var(--color-muted);">{inst.availability_zone}</td>
							<td class="px-4 py-3 whitespace-nowrap" title={inst.launch_time}>
								{fmtDate(inst.launch_time)}
							</td>
							<td class="px-4 py-3 whitespace-nowrap">
								{#if inst.first_started}
									<span title={inst.first_started}>{fmtDate(inst.first_started)}</span>
								{:else}
									<span
										title="No CloudTrail RunInstances event found — instance may be older than 90 days or CloudTrail access is restricted"
										style="color: var(--color-muted);"
										class="cursor-help"
									>N/A</span>
								{/if}
							</td>
							<td class="px-4 py-3">
								{#if inst.username}
									<span class="font-mono text-xs">{inst.username}</span>
								{:else}
									<span
										title="No CloudTrail RunInstances event found — instance may be older than 90 days or CloudTrail access is restricted"
										style="color: var(--color-muted);"
										class="cursor-help"
									>N/A</span>
								{/if}
							</td>
						</tr>
					{/each}
				{/if}
			</tbody>
		</table>
	</div>
</div>
