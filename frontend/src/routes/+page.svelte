<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { fetchS3Events, getConfig, getPricing, listCostResources, listEvents, listInstances, listRegions, listVolumes, loadCredConfig, rebootInstance, searchByTag, startInstance, stopInstance, terminateInstance, updateTags } from '$lib/api';
	import type { CostResource, EBSVolume, InstanceEvent, InstanceRecord, TagResource } from '$lib/types';
	import { estimateInstanceCostPerHour, estimateInstanceCostPerMonth, estimateEBSCostPerMonth, fmtCost, resourceTypeMeta } from '$lib/prices';

	let region = $state('us-east-1');
	let regions = $state<string[]>([]);
	let instances = $state<InstanceRecord[]>([]);
	let loading = $state(true);
	let refreshing = $state(false);
	let error = $state<string | null>(null);
	let autoRefresh = $state(true);
	let lastRefreshed = $state<Date | null>(null);
	let autoRefreshTimer: ReturnType<typeof setInterval> | null = null;

	let activeTab = $state<'instances' | 'timeline' | 'lifetime' | 'resources'>('instances');
	let extraRegions = $state<string[]>([]);
	let addRegionOpen = $state(false);
	let scanAllLoading = $state(false);

	// ── Live pricing (fetched from backend, keyed by region) ─────────────────
	let fetchedPrices = $state<Record<string, Record<string, number>>>({});

	async function loadPricing(r: string) {
		if (fetchedPrices[r]) return; // already loaded
		try {
			const result = await getPricing(r);
			if (result.count > 0) {
				fetchedPrices = { ...fetchedPrices, [r]: result.prices };
			}
		} catch { /* fall back to hardcoded estimates */ }
	}

	// Returns on-demand hourly rate: live price if available, otherwise hardcoded estimate.
	function hourlyRate(instanceType: string, instRegion: string): number | null {
		const live = fetchedPrices[instRegion]?.[instanceType];
		if (live != null) return live;
		return estimateInstanceCostPerHour(instanceType, instRegion);
	}

	function monthlyRate(instanceType: string, instRegion: string): number | null {
		const hr = hourlyRate(instanceType, instRegion);
		return hr != null ? hr * 730 : null;
	}

	function pricingSource(instRegion: string): 'live' | 'estimate' {
		return fetchedPrices[instRegion] ? 'live' : 'estimate';
	}

	// ── Tag / Resource search ─────────────────────────────────────────────────
	let tagKey = $state('');
	let tagValue = $state('');
	let tagResults = $state<TagResource[]>([]);
	let tagLoading = $state(false);
	let tagError = $state<string | null>(null);
	let tagSearched = $state(false);

	// ── EBS volumes ───────────────────────────────────────────────────────────
	let volumes = $state<EBSVolume[]>([]);
	let volumesLoading = $state(false);
	let volumesLoaded = $state(false);
	let volumesError = $state<string | null>(null);

	// ── Tag compliance ────────────────────────────────────────────────────────
	let requiredTagsInput = $state('');
	const requiredTags = $derived(
		requiredTagsInput.split(',').map(s => s.trim()).filter(Boolean)
	);

	// ── Cost inventory ───────────────────────────────────────────────────────
	let costInventoryMode = $state(false);
	let costResources = $state<CostResource[]>([]);
	let costResourcesLoading = $state(false);
	let costResourcesLoaded = $state(false);
	let costResourcesError = $state<string | null>(null);
	let costSortCol = $state<'cost' | 'name' | 'type'>('cost');
	let costFilterService = $state('');

	const filteredCostResources = $derived(
		[...costResources]
			.filter(r => !costFilterService || r.resource_type === costFilterService)
			.sort((a, b) =>
				costSortCol === 'cost' ? b.estimated_monthly_usd - a.estimated_monthly_usd
				: costSortCol === 'name' ? a.name.localeCompare(b.name)
				: a.resource_type.localeCompare(b.resource_type)
			)
	);

	const totalInventoryCost = $derived(
		filteredCostResources.reduce((s, r) => s + r.estimated_monthly_usd, 0)
	);

	const distinctResourceTypes = $derived(
		[...new Set(costResources.map(r => r.resource_type))].sort()
	);

	let events = $state<InstanceEvent[]>([]);
	let eventsLoading = $state(false);
	let eventsError = $state<string | null>(null);
	let eventsLoaded = $state(false);

	// ── Instance management ───────────────────────────────────────────────────
	let expandedId = $state<string | null>(null);
	let expandedVolumeId = $state<string | null>(null);
	let actionLoading = $state<string | null>(null);
	let actionError = $state<Record<string, string>>({});
	let confirmTerminateId = $state<string | null>(null);
	let confirmInput = $state('');
	let editingTagsId = $state<string | null>(null);
	let draftTags = $state<{ Key: string; Value: string }[]>([]);

	async function doAction(instanceId: string, action: 'start' | 'stop' | 'terminate' | 'reboot') {
		actionLoading = instanceId;
		actionError = { ...actionError, [instanceId]: '' };
		const t0 = Date.now();
		addLog('info', 'action', `${action} ${instanceId}…`);
		try {
			if (action === 'reboot') {
				await rebootInstance(region, instanceId);
				addLog('info', 'action', `${action} ${instanceId}: ok`, Date.now() - t0);
			} else {
				const fn = action === 'start' ? startInstance : action === 'stop' ? stopInstance : terminateInstance;
				const result = await fn(region, instanceId);
				addLog('info', 'action', `${action} ${instanceId}: ${result.previous_state} → ${result.current_state}`, Date.now() - t0);
			}
			confirmTerminateId = null;
			confirmInput = '';
			await loadInstances(true);
		} catch (e) {
			const msg = e instanceof Error ? e.message : String(e);
			actionError = { ...actionError, [instanceId]: msg };
			addLog('error', 'action', `${action} ${instanceId}: ${msg}`, Date.now() - t0);
		} finally {
			actionLoading = null;
		}
	}

	function startTagEdit(inst: { instance_id: string; tags: { Key: string; Value: string }[] | null }) {
		editingTagsId = inst.instance_id;
		draftTags = inst.tags ? inst.tags.map(t => ({ ...t })) : [];
	}

	async function saveTagEdit(instanceId: string, originalTags: { Key: string; Value: string }[] | null) {
		actionLoading = instanceId;
		const t0 = Date.now();
		const origKeys = new Set((originalTags ?? []).map(t => t.Key));
		const draftKeys = new Set(draftTags.map(t => t.Key));
		const deleteKeys = [...origKeys].filter(k => !draftKeys.has(k));
		addLog('info', 'tags', `Updating tags for ${instanceId}…`);
		try {
			await updateTags(region, instanceId, draftTags, deleteKeys);
			addLog('info', 'tags', `Tags updated for ${instanceId}`, Date.now() - t0);
			editingTagsId = null;
			await loadInstances(true);
		} catch (e) {
			const msg = e instanceof Error ? e.message : String(e);
			actionError = { ...actionError, [instanceId]: msg };
			addLog('error', 'tags', `Tag update ${instanceId}: ${msg}`, Date.now() - t0);
		} finally {
			actionLoading = null;
		}
	}

	// ── Log tray ─────────────────────────────────────────────────────────────

	interface LogEntry {
		id: number;
		time: Date;
		level: 'info' | 'warn' | 'error';
		source: string;
		message: string;
		duration?: number;
	}

	let logEntries = $state<LogEntry[]>([]);
	let logsOpen = $state(false);
	let logSeq = 0;
	let logScrollEl = $state<HTMLDivElement | null>(null);

	const errorCount = $derived(logEntries.filter(e => e.level === 'error').length);
	const latestEntry = $derived(logEntries.at(-1));

	function addLog(level: LogEntry['level'], source: string, message: string, duration?: number) {
		logEntries = [...logEntries.slice(-199), { id: logSeq++, time: new Date(), level, source, message, duration }];
	}

	$effect(() => {
		if (logsOpen && logScrollEl && logEntries.length > 0) {
			logScrollEl.scrollTop = logScrollEl.scrollHeight;
		}
	});

	function fmtTime(d: Date): string {
		return d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
	}

	function fmtDuration(ms: number): string {
		return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
	}

	// ── File import ───────────────────────────────────────────────────────────

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
		'shutting-down': '#f97316'
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

	function instanceLifetimeCost(inst: { instance_type: string; state: string; launch_time: string; last_stopped?: string | null; last_terminated?: string | null; region?: string }): number | null {
		const instRegion = inst.region ?? region;
		const monthly = monthlyRate(inst.instance_type, instRegion);
		if (monthly == null) return null;
		const hourlyRate = monthly / 730;
		const startMs = new Date(inst.launch_time).getTime();
		let endMs: number;
		if (inst.state === 'terminated') {
			endMs = inst.last_terminated ? new Date(inst.last_terminated).getTime() : Date.now();
		} else if (inst.state === 'stopped') {
			endMs = inst.last_stopped ? new Date(inst.last_stopped).getTime() : Date.now();
		} else {
			endMs = Date.now();
		}
		const hours = Math.max(0, (endMs - startMs) / 3_600_000);
		return hourlyRate * hours;
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

	function shortUsername(raw: string): string {
		// arn:aws:iam::123456789:user/john.doe → john.doe
		// arn:aws:sts::123456789:assumed-role/RoleName/session → RoleName/session
		const m = raw.match(/(?:user|assumed-role)\/(.+)$/);
		return m ? m[1] : raw;
	}

	const instanceNameMap = $derived(
		Object.fromEntries(instances.map(i => [i.instance_id, i.name]))
	);

	// Enrich instances with first_started/username from events (avoids per-instance CloudTrail calls)
	const firstRunMap = $derived(
		events.reduce((acc: Record<string, InstanceEvent>, e) => {
			if (e.event_name !== 'RunInstances') return acc;
			if (!acc[e.instance_id] || e.event_time < acc[e.instance_id].event_time) {
				acc[e.instance_id] = e;
			}
			return acc;
		}, {})
	);

	const lastStartedMap = $derived(
		events.reduce((acc: Record<string, InstanceEvent>, e) => {
			if (e.event_name !== 'StartInstances') return acc;
			if (!acc[e.instance_id] || e.event_time > acc[e.instance_id].event_time) acc[e.instance_id] = e;
			return acc;
		}, {})
	);

	const lastStoppedMap = $derived(
		events.reduce((acc: Record<string, InstanceEvent>, e) => {
			if (e.event_name !== 'StopInstances') return acc;
			if (!acc[e.instance_id] || e.event_time > acc[e.instance_id].event_time) acc[e.instance_id] = e;
			return acc;
		}, {})
	);

	const lastTerminatedMap = $derived(
		events.reduce((acc: Record<string, InstanceEvent>, e) => {
			if (e.event_name !== 'TerminateInstances') return acc;
			if (!acc[e.instance_id] || e.event_time > acc[e.instance_id].event_time) acc[e.instance_id] = e;
			return acc;
		}, {})
	);

	const enrichedInstances = $derived(
		instances.map(inst => ({
			...inst,
			first_started:      firstRunMap[inst.instance_id]?.event_time ?? null,
			username:           firstRunMap[inst.instance_id]?.username ?? null,
			last_started:       lastStartedMap[inst.instance_id]?.event_time ?? null,
			last_started_by:    lastStartedMap[inst.instance_id]?.username ?? null,
			last_stopped:       lastStoppedMap[inst.instance_id]?.event_time ?? null,
			last_stopped_by:    lastStoppedMap[inst.instance_id]?.username ?? null,
			last_terminated:    lastTerminatedMap[inst.instance_id]?.event_time ?? null,
			last_terminated_by: lastTerminatedMap[inst.instance_id]?.username ?? null,
		}))
	);

	// ── Search & Filter ──────────────────────────────────────────────────────────
	let searchText = $state('');
	let filterStates = $state<Set<string>>(new Set());
	let filterType = $state('');
	let filterAZ = $state('');
	let filterMissingTags = $state(false);

	const distinctTypes = $derived([...new Set(enrichedInstances.map(i => i.instance_type))].sort());
	const distinctAZs = $derived([...new Set(enrichedInstances.map(i => i.availability_zone))].sort());

	const filteredInstances = $derived(
		enrichedInstances.filter(inst => {
			if (searchText) {
				const q = searchText.toLowerCase();
				if (!inst.name.toLowerCase().includes(q) && !inst.instance_id.toLowerCase().includes(q)) return false;
			}
			if (filterStates.size > 0) {
				if (!filterStates.has(inst.state)) return false;
			}
			if (filterType && inst.instance_type !== filterType) return false;
			if (filterAZ && inst.availability_zone !== filterAZ) return false;
			if (filterMissingTags && requiredTags.length > 0) {
				const have = new Set((inst.tags ?? []).map(t => t.Key));
				if (requiredTags.every(k => have.has(k))) return false;
			}
			return true;
		})
	);

	const STD_STATES = ['running', 'stopped', 'terminated'];

	const stateCounts = $derived({
		running: enrichedInstances.filter(i => i.state === 'running').length,
		stopped: enrichedInstances.filter(i => i.state === 'stopped').length,
		terminated: enrichedInstances.filter(i => i.state === 'terminated').length,
		other: enrichedInstances.filter(i => !STD_STATES.includes(i.state)).length,
	});

	const transitionalCards = $derived(
		[...new Set(enrichedInstances.filter(i => !STD_STATES.includes(i.state)).map(i => i.state))]
			.sort()
			.map(state => ({
				state,
				label: state.charAt(0).toUpperCase() + state.slice(1),
				count: enrichedInstances.filter(i => i.state === state).length,
				color: stateColor(state),
			}))
	);

	const runningEstimatedCost = $derived(
		enrichedInstances
			.filter(i => i.state === 'running')
			.reduce((sum, i) => sum + (monthlyRate(i.instance_type, i.region ?? region) ?? 0), 0)
	);

	function toggleStateFilter(s: string) {
		const next = new Set(filterStates);
		next.has(s) ? next.delete(s) : next.add(s);
		filterStates = next;
	}

	function clearFilters() {
		searchText = '';
		filterStates = new Set();
		filterType = '';
		filterAZ = '';
		filterMissingTags = false;
	}

	const hasActiveFilters = $derived(
		searchText !== '' || filterStates.size > 0 || filterType !== '' || filterAZ !== '' || filterMissingTags
	);

	// ── Column Sorting ────────────────────────────────────────────────────────
	let sortCol = $state('name');
	let sortDir = $state<'asc' | 'desc'>('asc');

	function toggleSort(col: string) {
		if (sortCol === col) sortDir = sortDir === 'asc' ? 'desc' : 'asc';
		else { sortCol = col; sortDir = 'asc'; }
	}

	const sortedInstances = $derived(
		[...filteredInstances].sort((a, b) => {
			const dir = sortDir === 'asc' ? 1 : -1;
			let av = '', bv = '';
			if (sortCol === 'name') { av = a.name; bv = b.name; }
			else if (sortCol === 'id') { av = a.instance_id; bv = b.instance_id; }
			else if (sortCol === 'state') { av = a.state; bv = b.state; }
			else if (sortCol === 'type') { av = a.instance_type; bv = b.instance_type; }
			else if (sortCol === 'az') { av = a.availability_zone; bv = b.availability_zone; }
			else if (sortCol === 'launch') { av = a.launch_time ?? ''; bv = b.launch_time ?? ''; }
			else if (sortCol === 'started') { av = a.first_started ?? ''; bv = b.first_started ?? ''; }
			else if (sortCol === 'username') { av = a.username ?? ''; bv = b.username ?? ''; }
			else if (sortCol === 'stopped') { av = a.last_stopped ?? ''; bv = b.last_stopped ?? ''; }
			else if (sortCol === 'region') { av = a.region ?? ''; bv = b.region ?? ''; }
			return av.localeCompare(bv) * dir;
		})
	);

	// ── Column visibility ─────────────────────────────────────────────────────
	const ALL_COLUMNS = [
		{ key: 'name',       label: 'Name',         sort: 'name',     evt: false },
		{ key: 'region',     label: 'Region',        sort: 'region',   evt: false },
		{ key: 'id',         label: 'Instance ID',  sort: 'id',       evt: false },
		{ key: 'state',      label: 'State',        sort: 'state',    evt: false },
		{ key: 'type',       label: 'Type',         sort: 'type',     evt: false },
		{ key: 'az',         label: 'AZ',           sort: 'az',       evt: false },
		{ key: 'launch',     label: 'Launch Time',  sort: 'launch',   evt: false },
		{ key: 'started',    label: 'First Started',sort: 'started',  evt: true  },
		{ key: 'username',   label: 'Username',     sort: 'username', evt: true  },
		{ key: 'stopped',    label: 'Last Stopped', sort: 'stopped',  evt: true  },
		{ key: 'cost',       label: 'Est. Cost/mo', sort: null,       evt: false },
		{ key: 'compliance', label: 'Tag Compliance',sort: null,      evt: false },
		{ key: 'private_ip', label: 'Private IP',   sort: null,       evt: false },
		{ key: 'public_ip',  label: 'Public IP',    sort: null,       evt: false },
		{ key: 'vpc',        label: 'VPC',          sort: null,       evt: false },
		{ key: 'iam',        label: 'IAM Profile',  sort: null,       evt: false },
	];

	const DEFAULT_COLS = new Set(['name','id','state','type','az','launch','started','username','stopped']);

	// Auto-include 'region' column when multiple regions are loaded
	const effectiveVisibleCols = $derived.by(() => {
		const cols = new Set(visibleCols);
		if (extraRegions.length > 0) cols.add('region');
		else cols.delete('region');
		return cols;
	});

	let visibleCols = $state(new Set([...DEFAULT_COLS]));
	let colPickerOpen = $state(false);

	function toggleCol(key: string) {
		const next = new Set(visibleCols);
		if (next.has(key)) { if (next.size > 1) next.delete(key); }
		else next.add(key);
		visibleCols = next;
	}

	const tableColspan = $derived(effectiveVisibleCols.size + 1);

	// ── Waste / Cost ──────────────────────────────────────────────────────────
	const wasteFlags = $derived({
		longRunning: sortedInstances.filter(i => {
			if (i.state !== 'running') return false;
			return Date.now() - new Date(i.launch_time).getTime() > 30 * 86400000;
		}),
		stoppedInstances: sortedInstances.filter(i => i.state === 'stopped'),
		neverTagged: sortedInstances.filter(i => !(i.tags?.length)),
	});

	const unattachedVolumes = $derived(volumes.filter(v => v.state === 'available'));

	const estimatedMonthlyCost = $derived(
		sortedInstances
			.filter(i => i.state === 'running')
			.reduce((sum, i) => sum + (monthlyRate(i.instance_type, i.region ?? region) ?? 0), 0)
	);

	const unattachedEBSWaste = $derived(
		unattachedVolumes.reduce((sum, v) => sum + estimateEBSCostPerMonth(v.volume_type, v.size_gb, region), 0)
	);

	// ── Tag compliance per instance ───────────────────────────────────────────
	function missingTags(inst: typeof enrichedInstances[0]): string[] {
		if (!requiredTags.length) return [];
		const have = new Set((inst.tags ?? []).map(t => t.Key));
		return requiredTags.filter(k => !have.has(k));
	}

	// ── Copy to clipboard ─────────────────────────────────────────────────────
	let copiedValue = $state<string | null>(null);

	async function copyToClipboard(text: string, e: MouseEvent) {
		e.stopPropagation();
		await navigator.clipboard.writeText(text);
		copiedValue = text;
		setTimeout(() => { if (copiedValue === text) copiedValue = null; }, 1500);
	}

	// ── Active event list — file import overrides live API data
	const activeEvents = $derived(importSource === 'file' ? importedEvents : events);

	// ── Timeline Filters ──────────────────────────────────────────────────────
	let tlFilterFrom = $state('');
	let tlFilterTo = $state('');
	let tlFilterEvents = $state<Set<string>>(new Set());
	let tlFilterUser = $state('');
	let tlFilterInstanceId = $state('');

	function toggleTlEventFilter(name: string) {
		const next = new Set(tlFilterEvents);
		next.has(name) ? next.delete(name) : next.add(name);
		tlFilterEvents = next;
	}

	const STAT_CARDS: Array<{ state: keyof typeof stateCounts; label: string; color: string; icon: string; }> = [
		{ state: 'running',    label: 'Running',    color: '#22c55e', icon: 'M5 3l14 9-14 9V3z' },
		{ state: 'stopped',    label: 'Stopped',    color: '#eab308', icon: 'M6 4h4v16H6zM14 4h4v16h-4z' },
		{ state: 'terminated', label: 'Terminated', color: '#ef4444', icon: 'M18.364 18.364A9 9 0 0 0 5.636 5.636m12.728 12.728A9 9 0 0 1 5.636 5.636m12.728 12.728L5.636 5.636' },
	];

	const TABS: Array<{ id: 'instances'|'timeline'|'lifetime'|'resources'; label: string; icon: string; }> = [
		{ id: 'instances', label: 'Instances', icon: 'M3 12h18M3 6h18M3 18h18' },
		{ id: 'timeline', label: 'Timeline', icon: 'M12 3a9 9 0 1 0 0 18A9 9 0 0 0 12 3zM12 7v5l3.5 2' },
		{ id: 'lifetime', label: 'Lifetime', icon: 'M3 20V14h4V20H3zM10 20V9h4V20H10zM17 20V4h4V20H17z' },
		{ id: 'resources', label: 'Resources', icon: 'M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 17.93V18h-2v1.93C7.06 19.44 4.56 16.94 4.07 14H6v-2H4.07C4.56 9.06 7.06 6.56 10 6.07V8h2V6.07c2.94.49 5.44 2.99 5.93 5.93H16v2h1.93c-.49 2.94-2.99 5.44-5.93 5.93z' },
	];

	function jumpToTimeline(instanceId: string) {
		tlFilterInstanceId = instanceId;
		activeTab = 'timeline';
	}

	function jumpToInstance(instanceId: string) {
		activeTab = 'instances';
		expandedId = instanceId;
	}

	const filteredActiveEvents = $derived(
		activeEvents.filter(e => {
			if (tlFilterInstanceId && e.instance_id !== tlFilterInstanceId) return false;
			if (tlFilterFrom && e.event_time < tlFilterFrom) return false;
			if (tlFilterTo && e.event_time > tlFilterTo + 'T23:59:59Z') return false;
			if (tlFilterEvents.size > 0 && !tlFilterEvents.has(e.event_name)) return false;
			if (tlFilterUser) {
				const user = (e.username ?? '').toLowerCase();
				if (!user.includes(tlFilterUser.toLowerCase())) return false;
			}
			return true;
		})
	);

	const groupedEvents = $derived(
		Object.entries(
			filteredActiveEvents.reduce((acc: Record<string, InstanceEvent[]>, e) => {
				(acc[e.instance_id] ??= []).push(e);
				return acc;
			}, {})
		).sort(([a], [b]) =>
			(instanceNameMap[a] ?? a).localeCompare(instanceNameMap[b] ?? b)
		)
	);

	// ── S3 archive query ─────────────────────────────────────────────────────

	let s3PanelOpen = $state(false);
	let s3Bucket = $state('');
	let s3BucketRegion = $state('');
	let s3Prefix = $state('');
	let s3StartDate = $state('');
	let s3EndDate = $state('');
	let s3Loading = $state(false);
	let s3Error = $state<string | null>(null);

	function defaultStartDate(): string {
		const d = new Date();
		d.setFullYear(d.getFullYear() - 1);
		return d.toISOString().split('T')[0];
	}
	function defaultEndDate(): string {
		return new Date().toISOString().split('T')[0];
	}

	async function fetchS3Archive() {
		if (!s3Bucket.trim()) return;
		s3Loading = true;
		s3Error = null;
		const t0 = Date.now();
		const start = s3StartDate || defaultStartDate();
		const end = s3EndDate || defaultEndDate();
		addLog('info', 's3', `Fetching CloudTrail logs from s3://${s3Bucket.trim()} (${start} → ${end})…`);
		try {
			const s3Events = await fetchS3Events(region, {
				bucket: s3Bucket.trim(),
				bucketRegion: s3BucketRegion.trim() || undefined,
				prefix: s3Prefix.trim() || undefined,
				startDate: start,
				endDate: end,
			});
			// Merge with existing events, deduplicate by (event_time, event_name, instance_id)
			const existing = importSource === 'file' ? importedEvents : events;
			const seen = new Set(existing.map(e => `${e.event_time}|${e.event_name}|${e.instance_id}`));
			const newOnly = s3Events.filter(e => !seen.has(`${e.event_time}|${e.event_name}|${e.instance_id}`));
			const merged = [...existing, ...newOnly].sort((a, b) => a.event_time.localeCompare(b.event_time));
			importedEvents = merged;
			importSource = 'file';
			importFileName = `S3 archive (${s3Events.length} new events)`;
			activeTab = 'timeline';
			s3PanelOpen = false;
			addLog('info', 's3', `Fetched ${s3Events.length} events (${newOnly.length} new after dedup) from S3`, Date.now() - t0);
		} catch (e) {
			const msg = e instanceof Error ? e.message : String(e);
			s3Error = msg;
			addLog('error', 's3', msg, Date.now() - t0);
		} finally {
			s3Loading = false;
		}
	}

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
		if (!records) throw new Error('Unrecognised format — expected { Records: [...] } or Vantage JSON export');

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
		addLog('info', 'import', `Reading ${file.name} (${(file.size / 1024).toFixed(1)} KB)…`);
		const reader = new FileReader();
		const t0 = Date.now();
		reader.onload = () => {
			try {
				const parsed = parseCloudTrailJson(reader.result as string);
				importedEvents = parsed;
				importFileName = file.name;
				importSource = 'file';
				activeTab = 'timeline';
				addLog('info', 'import', `Parsed ${parsed.length} events from ${file.name}`, Date.now() - t0);
			} catch (err) {
				const msg = err instanceof Error ? err.message : String(err);
				importError = msg;
				importFileName = null;
				addLog('error', 'import', msg, Date.now() - t0);
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
		const allRegions = [region, ...extraRegions];
		const t0 = Date.now();
		addLog('info', 'instances', `Fetching instances for ${allRegions.join(', ')}…`);
		try {
			const results = await Promise.allSettled(allRegions.map(r => listInstances(r)));
			const merged: InstanceRecord[] = [];
			results.forEach((r, i) => {
				if (r.status === 'fulfilled') {
					merged.push(...r.value.map(inst => ({ ...inst, region: allRegions[i] })));
				} else {
					addLog('warn', 'instances', `Failed to load ${allRegions[i]}: ${r.reason instanceof Error ? r.reason.message : r.reason}`);
				}
			});
			instances = merged;
			lastRefreshed = new Date();
			addLog('info', 'instances', `Loaded ${instances.length} instance${instances.length !== 1 ? 's' : ''} from ${allRegions.length} region${allRegions.length !== 1 ? 's' : ''}`, Date.now() - t0);
			allRegions.forEach(r => loadPricing(r));
			if (!eventsLoaded && !eventsLoading && importSource === 'api') loadEventsData();
		} catch (e) {
			const msg = e instanceof Error ? e.message : String(e);
			error = msg;
			addLog('error', 'instances', msg, Date.now() - t0);
		} finally {
			loading = false;
			refreshing = false;
		}
	}

	async function scanAllRegions() {
		scanAllLoading = true;
		addLog('info', 'regions', 'Scanning all regions…');
		try {
			const all = await listRegions(region);
			extraRegions = all.filter(r => r !== region);
			await loadInstances();
		} catch (e) {
			addLog('warn', 'regions', `Scan all failed: ${e instanceof Error ? e.message : e}`);
		} finally {
			scanAllLoading = false;
		}
	}

	async function runTagSearch() {
		if (!tagKey.trim()) return;
		tagLoading = true;
		tagError = null;
		tagSearched = true;
		const t0 = Date.now();
		addLog('info', 'tag-search', `Searching for tag ${tagKey}${tagValue ? '=' + tagValue : ''} in ${region}…`);
		try {
			tagResults = await searchByTag(region, tagKey.trim(), tagValue.trim() || undefined);
			addLog('info', 'tag-search', `Found ${tagResults.length} resources`, Date.now() - t0);
		} catch (e) {
			const msg = e instanceof Error ? e.message : String(e);
			tagError = msg;
			addLog('error', 'tag-search', msg, Date.now() - t0);
		} finally {
			tagLoading = false;
		}
	}

	async function loadVolumes() {
		volumesLoading = true;
		volumesError = null;
		const t0 = Date.now();
		addLog('info', 'volumes', `Fetching EBS volumes for ${region}…`);
		try {
			volumes = await listVolumes(region);
			volumesLoaded = true;
			addLog('info', 'volumes', `Loaded ${volumes.length} volume${volumes.length !== 1 ? 's' : ''}`, Date.now() - t0);
		} catch (e) {
			const msg = e instanceof Error ? e.message : String(e);
			volumesError = msg;
			addLog('error', 'volumes', msg, Date.now() - t0);
		} finally {
			volumesLoading = false;
		}
	}

	async function loadCostInventory() {
		costResourcesLoading = true;
		costResourcesError = null;
		const t0 = Date.now();
		addLog('info', 'cost', `Fetching cost inventory for ${region}…`);
		try {
			costResources = await listCostResources(region);
			costResourcesLoaded = true;
			addLog('info', 'cost', `Loaded ${costResources.length} billable resource${costResources.length !== 1 ? 's' : ''}`, Date.now() - t0);
		} catch (e) {
			const msg = e instanceof Error ? e.message : String(e);
			costResourcesError = msg;
			addLog('error', 'cost', msg, Date.now() - t0);
		} finally {
			costResourcesLoading = false;
		}
	}

	async function loadEventsData() {
		eventsLoading = true;
		eventsError = null;
		const t0 = Date.now();
		addLog('info', 'events', `Fetching CloudTrail events for ${region}…`);
		try {
			events = await listEvents(region);
			eventsLoaded = true;
			addLog('info', 'events', `Loaded ${events.length} event${events.length !== 1 ? 's' : ''} from ${region}`, Date.now() - t0);
		} catch (e) {
			const msg = e instanceof Error ? e.message : String(e);
			eventsError = msg;
			addLog('error', 'events', msg, Date.now() - t0);
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

	async function switchTab(tab: 'instances' | 'timeline' | 'lifetime' | 'resources') {
		activeTab = tab;
		if ((tab === 'timeline' || tab === 'lifetime') && importSource === 'api' && !eventsLoaded && !eventsLoading) {
			await loadEventsData();
		}
	}

	function onCredentialsSaved() {
		const stored = loadCredConfig();
		if (stored.region) {
			region = stored.region;
			if (!regions.includes(region)) regions = [region, ...regions];
		}
		addLog('info', 'credentials', `Credential source changed to ${stored.source}${stored.region ? ` · region ${stored.region}` : ''}`);
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
			`vantage-timeline-${region}-${today()}.json`
		);
	}

	function downloadCSV() {
		const rows: string[][] = [['event_time', 'event_name', 'instance_id', 'instance_name', 'username', 'source_ip']];
		for (const e of activeEvents) {
			rows.push([e.event_time, e.event_name, e.instance_id, instanceNameMap[e.instance_id] ?? e.instance_id, e.username ?? '', e.source_ip ?? '']);
		}
		const csv = rows.map(r => r.map(c => `"${c.replace(/"/g, '""')}"`).join(',')).join('\n');
		triggerDownload(new Blob([csv], { type: 'text/csv' }), `vantage-timeline-${region}-${today()}.csv`);
	}

	function exportInstancesCSV() {
		const headers = ['Name','Instance ID','State','Type','AZ','Launch Time','First Started','Username','Private IP','Public IP','VPC','Subnet','AMI','Key Pair','IAM Profile','Architecture','Tags'];
		const rows: string[][] = [headers];
		for (const i of sortedInstances) {
			rows.push([
				i.name, i.instance_id, i.state, i.instance_type, i.availability_zone,
				i.launch_time, i.first_started ?? '', i.username ?? '',
				i.private_ip ?? '', i.public_ip ?? '', i.vpc_id ?? '', i.subnet_id ?? '',
				i.image_id ?? '', i.key_name ?? '', i.iam_profile ?? '', i.architecture ?? '',
				(i.tags ?? []).map(t => `${t.Key}=${t.Value}`).join(';'),
			]);
		}
		const csv = rows.map(r => r.map(c => `"${c.replace(/"/g, '""')}"`).join(',')).join('\n');
		triggerDownload(new Blob([csv], { type: 'text/csv' }), `vantage-instances-${region}-${today()}.csv`);
	}

	async function exportInstancesExcel() {
		const XLSX = await import('xlsx');
		const wb = XLSX.utils.book_new();

		// Sheet 1 — Summary
		const summaryRows = [
			['Vantage — Instance Report'],
			['Region', region],
			['Generated', new Date().toLocaleString()],
			[],
			['State', 'Count'],
			['Running',    stateCounts.running],
			['Stopped',    stateCounts.stopped],
			['Terminated', stateCounts.terminated],
			['Other',      stateCounts.other],
			['Total (filtered)', sortedInstances.length],
		];
		XLSX.utils.book_append_sheet(wb, XLSX.utils.aoa_to_sheet(summaryRows), 'Summary');

		// Sheet 2 — Instances
		const instHeaders = ['Name','Instance ID','State','Type','AZ','Launch Time','First Started','First Started By','Last Started','Last Started By','Last Stopped','Last Stopped By','Terminated','Terminated By','Private IP','Public IP','VPC','Subnet','AMI','Key Pair','IAM Profile','Architecture','Tags'];
		const instRows = sortedInstances.map(i => [
			i.name, i.instance_id, i.state, i.instance_type, i.availability_zone,
			i.launch_time, i.first_started ?? '', i.username ?? '',
			i.last_started ?? '', i.last_started_by ?? '',
			i.last_stopped ?? '', i.last_stopped_by ?? '',
			i.last_terminated ?? '', i.last_terminated_by ?? '',
			i.private_ip ?? '', i.public_ip ?? '', i.vpc_id ?? '', i.subnet_id ?? '',
			i.image_id ?? '', i.key_name ?? '', i.iam_profile ?? '', i.architecture ?? '',
			(i.tags ?? []).map(t => `${t.Key}=${t.Value}`).join(';'),
		]);
		XLSX.utils.book_append_sheet(wb, XLSX.utils.aoa_to_sheet([instHeaders, ...instRows]), 'Instances');

		// Sheet 3 — Events (only if loaded)
		if (eventsLoaded && activeEvents.length > 0) {
			const evtHeaders = ['Time','Event','Instance ID','Instance Name','Username','Source IP'];
			const evtRows = activeEvents.map(e => [
				e.event_time, e.event_name, e.instance_id,
				instanceNameMap[e.instance_id] ?? e.instance_id,
				e.username ?? '', e.source_ip ?? '',
			]);
			XLSX.utils.book_append_sheet(wb, XLSX.utils.aoa_to_sheet([evtHeaders, ...evtRows]), 'Events');
		}

		XLSX.writeFile(wb, `vantage-report-${region}-${today()}.xlsx`);
	}

	async function exportInstancesPDF() {
		const { jsPDF } = await import('jspdf');
		const { default: autoTable } = await import('jspdf-autotable');

		const doc = new jsPDF({ orientation: 'landscape', unit: 'mm', format: 'a4' });
		const d = () => (doc as unknown as { lastAutoTable: { finalY: number } }).lastAutoTable?.finalY ?? 30;

		// Header
		doc.setFontSize(16); doc.setTextColor(30);
		doc.text('Vantage — Instance Report', 14, 15);
		doc.setFontSize(9); doc.setTextColor(100);
		doc.text(`Region: ${region}`, 14, 21);
		doc.text(`Generated: ${new Date().toLocaleString()}`, 14, 26);

		// Summary
		doc.setFontSize(11); doc.setTextColor(30);
		doc.text('Summary', 14, 34);
		autoTable(doc, {
			startY: 37,
			head: [['Running', 'Stopped', 'Terminated', 'Other', 'Total (filtered)']],
			body: [[stateCounts.running, stateCounts.stopped, stateCounts.terminated, stateCounts.other, sortedInstances.length]],
			theme: 'grid',
			headStyles: { fillColor: [41, 98, 180], fontSize: 9 },
			bodyStyles: { fontSize: 9 },
			tableWidth: 130,
		});

		// Instances table
		const instY = d() + 8;
		doc.setFontSize(11); doc.setTextColor(30);
		doc.text('Instances', 14, instY);
		autoTable(doc, {
			startY: instY + 3,
			head: [['Name','Instance ID','State','Type','AZ','Launch Time','First Started','Last Started','Last Stopped']],
			body: sortedInstances.map(i => [
				i.name, i.instance_id, i.state, i.instance_type, i.availability_zone,
				fmtDate(i.launch_time) || '—',
				fmtDate(i.first_started) || '—',
				fmtDate(i.last_started)  || '—',
				fmtDate(i.last_stopped)  || '—',
			]),
			theme: 'striped',
			headStyles: { fillColor: [41, 98, 180], fontSize: 8 },
			bodyStyles: { fontSize: 7 },
			alternateRowStyles: { fillColor: [245, 247, 250] },
		});

		// Events page (only if loaded)
		if (eventsLoaded && activeEvents.length > 0) {
			doc.addPage();
			doc.setFontSize(16); doc.setTextColor(30);
			doc.text('CloudTrail Events', 14, 15);
			doc.setFontSize(9); doc.setTextColor(100);
			doc.text(`Region: ${region}  ·  ${activeEvents.length} events`, 14, 21);
			autoTable(doc, {
				startY: 26,
				head: [['Time','Event','Instance ID','Instance Name','Username','Source IP']],
				body: activeEvents.map(e => [
					fmtDate(e.event_time), e.event_name, e.instance_id,
					instanceNameMap[e.instance_id] ?? e.instance_id,
					e.username ?? '—', e.source_ip ?? '—',
				]),
				theme: 'striped',
				headStyles: { fillColor: [41, 98, 180], fontSize: 8 },
				bodyStyles: { fontSize: 7 },
				alternateRowStyles: { fillColor: [245, 247, 250] },
			});
		}

		doc.save(`vantage-report-${region}-${today()}.pdf`);
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
		// Apply stored region immediately so the dropdown is never blank
		const stored = loadCredConfig();
		region = stored.region || 'us-east-1';
		regions = [region];

		// Start loading instances right away — don't block on regions/config fetch
		loadInstances();
		startAutoRefresh();
		window.addEventListener('vantage:credentials-saved', onCredentialsSaved);

		// Populate region dropdown and server-default region in the background
		getConfig()
			.then(cfg => { if (!stored.region) region = cfg.default_region; })
			.catch(e => addLog('warn', 'config', `Could not fetch server config: ${e instanceof Error ? e.message : e}`));

		listRegions(region)
			.then(regs => {
				regions = regs.includes(region) ? regs : [region, ...regs];
				addLog('info', 'regions', `Loaded ${regs.length} regions`);
			})
			.catch(e => addLog('warn', 'regions', `Could not fetch region list: ${e instanceof Error ? e.message : e}`));
	});

	onDestroy(() => {
		stopAutoRefresh();
		window.removeEventListener('vantage:credentials-saved', onCredentialsSaved);
	});

	// ── Gantt chart ───────────────────────────────────────────────────────────

	const GANTT_ROW_H = 36;
	const GANTT_BAR_H = 18;
	const GANTT_HDR_H = 32;
	const GANTT_MIN_W = 900;

	interface GanttSegment { start: number; end: number; state: 'running' | 'stopped' | 'terminated'; dashed: boolean; }
	interface GanttRow { instanceId: string; name: string; currentState: string; segments: GanttSegment[]; }
	interface GanttTooltipData { x: number; y: number; row: GanttRow; seg: GanttSegment; }

	let ganttZoomFrom = $state('');
	let ganttZoomTo = $state('');
	let ganttTooltip = $state<GanttTooltipData | null>(null);
	let ganttScrollEl = $state<HTMLDivElement | null>(null);
	let ganttScrollElWidth = $state(0);

	$effect(() => {
		const el = ganttScrollEl;
		if (!el) return;
		const obs = new ResizeObserver(() => { ganttScrollElWidth = el.clientWidth; });
		obs.observe(el);
		ganttScrollElWidth = el.clientWidth;
		return () => obs.disconnect();
	});

	const ganttSvgWidth = $derived(Math.max(ganttScrollElWidth - 4, GANTT_MIN_W));

	function computeGantt(
		insts: Array<typeof enrichedInstances[0]>,
		evts: InstanceEvent[]
	): GanttRow[] {
		const now = Date.now();
		const byInst: Record<string, InstanceEvent[]> = {};
		for (const e of evts) (byInst[e.instance_id] ??= []).push(e);
		for (const k of Object.keys(byInst)) byInst[k].sort((a, b) => a.event_time.localeCompare(b.event_time));

		const instMap = Object.fromEntries(insts.map(i => [i.instance_id, i]));
		const allIds = new Set([...insts.map(i => i.instance_id), ...Object.keys(byInst)]);
		const rows: GanttRow[] = [];

		for (const id of allIds) {
			const inst = instMap[id];
			const events = byInst[id] ?? [];
			const segs: GanttSegment[] = [];

			if (events.length === 0) {
				if (!inst) continue;
				const start = new Date(inst.launch_time).getTime();
				const st: GanttSegment['state'] = inst.state === 'running' ? 'running' : inst.state === 'terminated' ? 'terminated' : 'stopped';
				const end = st === 'terminated' ? start : now;
				segs.push({ start, end, state: st, dashed: true });
			} else {
				let curState: GanttSegment['state'] | null = null;
				let curStart = 0;
				let dashed = false;

				for (const evt of events) {
					const t = new Date(evt.event_time).getTime();
					const close = () => { if (curState) segs.push({ start: curStart, end: t, state: curState, dashed }); dashed = false; };

					if (evt.event_name === 'RunInstances') {
						close(); curState = 'running'; curStart = t;
					} else if (evt.event_name === 'StopInstances') {
						if (!curState) { curState = 'running'; curStart = t; dashed = true; }
						close(); curState = 'stopped'; curStart = t;
					} else if (evt.event_name === 'StartInstances') {
						if (!curState) { curState = 'stopped'; curStart = t; dashed = true; }
						close(); curState = 'running'; curStart = t;
					} else if (evt.event_name === 'TerminateInstances') {
						if (!curState) { curState = 'running'; curStart = t; dashed = true; }
						close();
						segs.push({ start: t, end: t, state: 'terminated', dashed: false });
						curState = null;
					}
				}
				if (curState && curState !== 'terminated') segs.push({ start: curStart, end: now, state: curState, dashed });
			}

			if (segs.length > 0) {
				rows.push({ instanceId: id, name: inst?.name ?? id, currentState: inst?.state ?? 'terminated', segments: segs });
			}
		}
		return rows.sort((a, b) => a.name.localeCompare(b.name));
	}

	const ganttRows = $derived(computeGantt(enrichedInstances, activeEvents));

	const ganttFullRange = $derived.by(() => {
		let min = Infinity, max = -Infinity;
		for (const row of ganttRows) {
			for (const s of row.segments) {
				if (s.start < min) min = s.start;
				if (s.end > max) max = s.end;
			}
		}
		if (!isFinite(min)) { min = Date.now() - 86400000 * 90; max = Date.now(); }
		return { min, max };
	});

	const ganttViewRange = $derived.by(() => {
		const f = ganttFullRange;
		const from = ganttZoomFrom ? new Date(ganttZoomFrom).getTime() : f.min;
		const to = ganttZoomTo ? new Date(ganttZoomTo + 'T23:59:59Z').getTime() : f.max;
		return { min: from, max: Math.max(to, from + 3600000) };
	});

	function ganttTicks(minMs: number, maxMs: number, width: number): Array<{ x: number; label: string }> {
		const range = maxMs - minMs;
		const targetTicks = Math.max(3, Math.floor(width / 110));
		const rawInterval = range / targetTicks;
		const STEPS = [3600e3, 21600e3, 86400e3, 7 * 86400e3, 30 * 86400e3, 90 * 86400e3, 365 * 86400e3];
		const interval = STEPS.find(s => s >= rawInterval) ?? STEPS[STEPS.length - 1];
		const ticks: Array<{ x: number; label: string }> = [];
		const start = Math.ceil(minMs / interval) * interval;
		for (let t = start; t <= maxMs; t += interval) {
			const d = new Date(t);
			const label = interval < 86400e3
				? d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) + ' ' + d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false })
				: interval < 30 * 86400e3
					? d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
					: d.toLocaleDateString('en-US', { month: 'short', year: 'numeric' });
			ticks.push({ x: ((t - minMs) / (maxMs - minMs)) * width, label });
		}
		return ticks;
	}

	function fmtDuration2(ms: number): string {
		const d = Math.floor(ms / 86400000);
		const h = Math.floor((ms % 86400000) / 3600000);
		const m = Math.floor((ms % 3600000) / 60000);
		if (d > 0) return `${d}d ${h}h`;
		if (h > 0) return `${h}h ${m}m`;
		return `${m}m`;
	}
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
	<div class="flex items-center gap-2 flex-wrap">
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
		{#each extraRegions as r}
			<span class="flex items-center gap-1 rounded px-2 py-0.5 text-xs font-medium" style="background-color: var(--color-accent)22; color: var(--color-accent); border: 1px solid var(--color-accent)44;">
				{r}
				<button onclick={() => { extraRegions = extraRegions.filter(x => x !== r); loadInstances(); }} style="opacity:0.7;" title="Remove region">✕</button>
			</span>
		{/each}
		<div class="relative">
			<button
				onclick={() => addRegionOpen = !addRegionOpen}
				class="text-xs rounded px-2 py-1 border transition-colors"
				style="border-color: var(--color-border); color: var(--color-muted);"
				title="Add another region"
			>+ Region</button>
			{#if addRegionOpen}
				<div class="fixed inset-0 z-40" role="presentation" onclick={() => addRegionOpen = false}></div>
				<div class="absolute left-0 top-full mt-1 z-50 rounded border shadow-xl py-1 max-h-64 overflow-y-auto min-w-44"
					style="background-color: var(--color-surface); border-color: var(--color-border);">
					{#each regions.filter(r => r !== region && !extraRegions.includes(r)) as r}
						<button
							onclick={() => { extraRegions = [...extraRegions, r]; addRegionOpen = false; loadInstances(); }}
							class="block w-full text-left px-3 py-1.5 text-xs hover:opacity-80"
						>{r}</button>
					{/each}
				</div>
			{/if}
		</div>
		<button
			onclick={scanAllRegions}
			disabled={scanAllLoading}
			class="text-xs rounded px-2 py-1 border transition-colors disabled:opacity-50"
			style="border-color: var(--color-border); color: var(--color-muted);"
			title="Query all available regions simultaneously"
		>{scanAllLoading ? 'Scanning…' : 'Scan All'}</button>
		{#if extraRegions.length > 0}
			<button onclick={() => { extraRegions = []; loadInstances(); }} class="text-xs" style="color: var(--color-muted);">Clear</button>
		{/if}
	</div>

	{#if activeTab === 'instances'}
		<button
			onclick={() => loadInstances()}
			disabled={loading || refreshing}
			class="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium transition-colors disabled:opacity-50"
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

		<div class="w-px h-5 self-center shrink-0" style="background-color: var(--color-border);"></div>
		<!-- Column picker -->
		<div class="relative">
			<button
				onclick={() => colPickerOpen = !colPickerOpen}
				class="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium border transition-colors"
				style="border-color: {colPickerOpen ? 'var(--color-accent)' : 'var(--color-border)'}; color: var(--color-text);"
				title="Show/hide columns"
			>
				<svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
					<rect x="3" y="3" width="7" height="18" rx="1"/><rect x="14" y="3" width="7" height="18" rx="1"/>
				</svg>
				Columns
				<svg class="w-3 h-3 transition-transform" style="transform: rotate({colPickerOpen ? '180deg' : '0deg'});" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
			</button>
			{#if colPickerOpen}
				<div class="fixed inset-0 z-40" role="presentation" onclick={() => colPickerOpen = false}></div>
				<div class="absolute left-0 top-full mt-1 z-50 rounded border shadow-xl py-1 min-w-44"
					style="background-color: var(--color-surface); border-color: var(--color-border);">
					{#each ALL_COLUMNS as col}
						<label class="flex items-center gap-2.5 px-3 py-1.5 text-xs cursor-pointer select-none hover:opacity-80">
							<input
								type="checkbox"
								checked={effectiveVisibleCols.has(col.key)}
								onchange={() => toggleCol(col.key)}
								class="rounded"
							/>
							{col.label}
						</label>
					{/each}
					<div class="border-t mt-1 pt-1 px-3 pb-1" style="border-color: var(--color-border);">
						<button
							onclick={() => { visibleCols = new Set(['name','id','state','type','az','launch','started','username','stopped']); }}
							class="text-xs" style="color: var(--color-muted);">Reset to defaults</button>
					</div>
				</div>
			{/if}
		</div>

		{#if sortedInstances.length > 0}
			<button
				onclick={exportInstancesCSV}
				class="flex items-center gap-1.5 rounded px-3 py-1.5 text-sm font-medium border transition-colors"
				style="border-color: var(--color-border); color: var(--color-text);"
				title="Export filtered instances as CSV"
			>
				<svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
					<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>
				</svg>
				CSV
			</button>
			<button
				onclick={exportInstancesExcel}
				class="flex items-center gap-1.5 rounded px-3 py-1.5 text-sm font-medium border transition-colors"
				style="border-color: var(--color-border); color: var(--color-text);"
				title="Export report as Excel (.xlsx) — includes Summary, Instances, and Events sheets"
			>
				<svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
					<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>
					<line x1="8" y1="13" x2="16" y2="13"/><line x1="8" y1="17" x2="16" y2="17"/><line x1="10" y1="9" x2="8" y2="9"/>
				</svg>
				Excel
			</button>
			<button
				onclick={exportInstancesPDF}
				class="flex items-center gap-1.5 rounded px-3 py-1.5 text-sm font-medium border transition-colors"
				style="border-color: var(--color-border); color: var(--color-text);"
				title="Export report as PDF — includes summary stats, instance table, and events (if loaded)"
			>
				<svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
					<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>
					<line x1="9" y1="15" x2="15" y2="15"/>
				</svg>
				PDF
			</button>
		{/if}

		<span class="text-sm ml-auto" style="color: var(--color-muted);">
			{#if !loading}
				{#if hasActiveFilters}
					{sortedInstances.length} of {instances.length} instance{instances.length !== 1 ? 's' : ''}
				{:else}
					{instances.length} instance{instances.length !== 1 ? 's' : ''}
				{/if}
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

		<!-- S3 archive query -->
		<div class="relative">
			<button
				onclick={() => { s3PanelOpen = !s3PanelOpen; s3Error = null; }}
				class="flex items-center gap-1.5 rounded px-3 py-1.5 text-sm font-medium border transition-colors"
				style="border-color: var(--color-border); color: var(--color-text);"
				title="Query CloudTrail logs archived in S3 (beyond 90-day lookup limit)"
			>
				<svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
					<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/>
				</svg>
				S3 Archive
				<svg class="w-3 h-3 transition-transform" style="transform: rotate({s3PanelOpen ? '180deg' : '0deg'});" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
			</button>

			{#if s3PanelOpen}
				<div class="absolute left-0 top-full mt-1 z-50 rounded-lg border p-4 shadow-xl"
					style="background-color: var(--color-surface); border-color: var(--color-border); width: 340px;">
					<p class="text-xs font-semibold mb-3" style="color: var(--color-muted);">QUERY S3 CLOUDTRAIL ARCHIVE</p>
					<p class="text-xs mb-3" style="color: var(--color-muted);">
						Fetches logs beyond the 90-day <code>lookup_events</code> limit.
						Path: <code>AWSLogs/&#123;account&#125;/CloudTrail/&#123;region&#125;/…</code>
					</p>

					<label class="block mb-2">
						<span class="text-xs mb-1 block" style="color: var(--color-muted);">S3 Bucket <span style="color: #f87171;">*</span></span>
						<input type="text" bind:value={s3Bucket} placeholder="my-cloudtrail-bucket"
							class="w-full rounded px-2.5 py-1.5 text-sm border font-mono"
							style="background-color: var(--color-bg); border-color: var(--color-border); color: var(--color-text);" />
					</label>

					<label class="block mb-2">
						<span class="text-xs mb-1 block" style="color: var(--color-muted);">Bucket Region <span class="opacity-50">(if different from {region})</span></span>
						<input type="text" bind:value={s3BucketRegion} placeholder={region}
							class="w-full rounded px-2.5 py-1.5 text-sm border font-mono"
							style="background-color: var(--color-bg); border-color: var(--color-border); color: var(--color-text);" />
					</label>

					<label class="block mb-2">
						<span class="text-xs mb-1 block" style="color: var(--color-muted);">Prefix <span class="opacity-50">(optional — for org trails)</span></span>
						<input type="text" bind:value={s3Prefix} placeholder="e.g. org-trail/"
							class="w-full rounded px-2.5 py-1.5 text-sm border font-mono"
							style="background-color: var(--color-bg); border-color: var(--color-border); color: var(--color-text);" />
					</label>

					<div class="flex gap-2 mb-3">
						<label class="flex-1">
							<span class="text-xs mb-1 block" style="color: var(--color-muted);">From</span>
							<input type="date" bind:value={s3StartDate} placeholder={defaultStartDate()}
								class="w-full rounded px-2.5 py-1.5 text-sm border"
								style="background-color: var(--color-bg); border-color: var(--color-border); color: var(--color-text);" />
						</label>
						<label class="flex-1">
							<span class="text-xs mb-1 block" style="color: var(--color-muted);">To</span>
							<input type="date" bind:value={s3EndDate} placeholder={defaultEndDate()}
								class="w-full rounded px-2.5 py-1.5 text-sm border"
								style="background-color: var(--color-bg); border-color: var(--color-border); color: var(--color-text);" />
						</label>
					</div>

					{#if s3Error}
						<p class="text-xs mb-3 rounded px-2 py-1.5" style="background-color: var(--color-error-bg); color: var(--color-error-text);">{s3Error}</p>
					{/if}

					<div class="flex gap-2">
						<button
							onclick={fetchS3Archive}
							disabled={s3Loading || !s3Bucket.trim()}
							class="flex-1 flex items-center justify-center gap-1.5 rounded px-3 py-1.5 text-sm font-medium transition-colors disabled:opacity-40"
							style="background-color: var(--color-accent); color: white;"
						>
							{#if s3Loading}
								<svg class="w-3.5 h-3.5 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 11-6.219-8.56"/></svg>
								Fetching…
							{:else}
								Fetch Events
							{/if}
						</button>
						<button onclick={() => s3PanelOpen = false}
							class="rounded px-3 py-1.5 text-sm border transition-colors"
							style="border-color: var(--color-border); color: var(--color-muted);">
							Cancel
						</button>
					</div>
				</div>
				<div class="fixed inset-0 z-40" role="presentation" onclick={() => s3PanelOpen = false}></div>
			{/if}
		</div>

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
				{filteredActiveEvents.length}{filteredActiveEvents.length !== activeEvents.length ? ` of ${activeEvents.length}` : ''} event{filteredActiveEvents.length !== 1 ? 's' : ''} across {groupedEvents.length} instance{groupedEvents.length !== 1 ? 's' : ''}
				{#if importSource === 'file'}<span class="ml-1" style="color: #a5b4fc;">· from file</span>{/if}
			{/if}
		</span>
	{/if}
</div>

<!-- Tab bar -->
<div class="flex items-center gap-1 mt-4 mb-5 p-1 rounded-xl border w-fit" style="background-color: var(--color-surface); border-color: var(--color-border);">
	{#each TABS as t}
		<button
			onclick={() => switchTab(t.id)}
			class="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium transition-all"
			style="{activeTab === t.id ? 'background-color: var(--color-accent-dim); color: var(--color-accent); border: 1px solid rgba(99,102,241,0.25);' : 'color: var(--color-muted); border: 1px solid transparent;'}"
			onmouseenter={(e) => { if (activeTab !== t.id) (e.currentTarget as HTMLElement).style.backgroundColor = 'var(--color-surface-raised)'; }}
			onmouseleave={(e) => { if (activeTab !== t.id) (e.currentTarget as HTMLElement).style.backgroundColor = ''; }}
		>
			<svg class="w-3.5 h-3.5 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d={t.icon}/></svg>
			{t.label}{#if t.id === 'timeline' && importSource === 'file'}<span class="ml-0.5 w-1.5 h-1.5 rounded-full inline-block" style="background-color: var(--color-accent); vertical-align: middle;"></span>{/if}
		</button>
	{/each}
</div>

<!-- ── INSTANCES TAB ── -->
{#if activeTab === 'instances'}
	{#if error}
		<div class="mb-4 rounded-xl border px-4 py-3 text-sm" style="background-color: var(--color-error-bg); border-color: var(--color-error-border); color: var(--color-error-text);">
			<strong>AWS Error:</strong> {error}
		</div>
	{/if}

	<!-- Search & filter bar -->
	<div class="flex flex-wrap items-center gap-2 mb-3">
		<div class="relative flex-1 min-w-48">
			<svg class="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 pointer-events-none" style="color: var(--color-muted);" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>
			<input
				type="text"
				placeholder="Search name or ID…"
				bind:value={searchText}
				class="w-full rounded-lg pl-8 pr-3 py-1.5 text-sm border"
				style="background-color: var(--color-surface); border-color: var(--color-border); color: var(--color-text);"
			/>
		</div>
		<select bind:value={filterType} class="rounded-lg px-2 py-1.5 text-sm border" style="background-color: var(--color-surface); border-color: var(--color-border); color: var(--color-text);">
			<option value="">All types</option>
			{#each distinctTypes as t}<option value={t}>{t}</option>{/each}
		</select>
		<select bind:value={filterAZ} class="rounded-lg px-2 py-1.5 text-sm border" style="background-color: var(--color-surface); border-color: var(--color-border); color: var(--color-text);">
			<option value="">All AZs</option>
			{#each distinctAZs as az}<option value={az}>{az}</option>{/each}
		</select>
		<input
			type="text"
			placeholder="Required tags (Owner,Env…)"
			bind:value={requiredTagsInput}
			class="rounded-lg px-2 py-1.5 text-sm border w-44"
			style="background-color: var(--color-surface); border-color: var(--color-border); color: var(--color-text);"
			title="Comma-separated tag keys that all instances should have. Enables compliance column and filter."
		/>
		{#if requiredTags.length > 0}
			<button
				onclick={() => filterMissingTags = !filterMissingTags}
				class="text-xs rounded px-2.5 py-1.5 border transition-colors"
				style="border-color: {filterMissingTags ? '#f59e0b' : 'var(--color-border)'}; color: {filterMissingTags ? '#f59e0b' : 'var(--color-muted)'};"
				title="Show only instances missing required tags"
			>Non-compliant</button>
		{/if}
		{#if hasActiveFilters}
			<button onclick={clearFilters} class="text-xs rounded px-2.5 py-1.5 border transition-colors" style="border-color: var(--color-border); color: var(--color-muted);">Clear filters</button>
		{/if}
	</div>

	<!-- Stat cards -->
	{#if !loading && instances.length > 0}
		<div class="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-5">
			{#each STAT_CARDS as card}
				{@const count = stateCounts[card.state]}
				{@const active = filterStates.has(card.state)}
				{#if count > 0 || card.state === 'running'}
					<button
						onclick={() => toggleStateFilter(card.state)}
						class="rounded-xl p-4 border text-left transition-all"
						style="background-color: {active ? card.color + '18' : 'var(--color-surface)'}; border-color: {active ? card.color + '66' : 'var(--color-border)'};"
						onmouseenter={(e) => { if (!active) (e.currentTarget as HTMLElement).style.borderColor = card.color + '55'; }}
						onmouseleave={(e) => { if (!active) (e.currentTarget as HTMLElement).style.borderColor = active ? card.color + '66' : 'var(--color-border)'; }}
					>
						<div class="flex items-start justify-between mb-2">
							<span class="text-xs font-semibold uppercase tracking-wide" style="color: {card.color};">{card.label}</span>
							<svg class="w-4 h-4 shrink-0" style="color: {card.color}; opacity: 0.7;" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d={card.icon}/></svg>
						</div>
						<p class="text-2xl font-bold tracking-tight">{count}</p>
						{#if card.state === 'running' && runningEstimatedCost > 0}
							<p class="text-xs mt-1" style="color: var(--color-muted);">~{fmtCost(runningEstimatedCost)}/mo</p>
						{:else if card.state === 'stopped' && count > 0}
							<p class="text-xs mt-1" style="color: var(--color-muted);">EBS costs</p>
						{/if}
					</button>
				{/if}
			{/each}
			<!-- Dynamic transitional state cards (one per distinct state) -->
			{#each transitionalCards as card}
				{@const active = filterStates.has(card.state)}
				<button
					onclick={() => toggleStateFilter(card.state)}
					class="rounded-xl p-4 border text-left transition-all"
					style="background-color: {active ? card.color + '18' : 'var(--color-surface)'}; border-color: {active ? card.color + '66' : 'var(--color-border)'};"
					onmouseenter={(e) => { if (!active) (e.currentTarget as HTMLElement).style.borderColor = card.color + '55'; }}
					onmouseleave={(e) => { if (!active) (e.currentTarget as HTMLElement).style.borderColor = active ? card.color + '66' : 'var(--color-border)'; }}
				>
					<div class="flex items-start justify-between mb-2">
						<span class="text-xs font-semibold uppercase tracking-wide" style="color: {card.color};">{card.label}</span>
						<!-- Transition/spinner icon -->
						<svg class="w-4 h-4 shrink-0" style="color: {card.color}; opacity: 0.7;" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg>
					</div>
					<p class="text-2xl font-bold tracking-tight">{card.count}</p>
					<p class="text-xs mt-1" style="color: var(--color-muted);">transitioning</p>
				</button>
			{/each}
			<!-- Total card -->
			<div class="rounded-xl p-4 border" style="background-color: var(--color-surface); border-color: var(--color-border);">
				<div class="flex items-start justify-between mb-2">
					<span class="text-xs font-semibold uppercase tracking-wide" style="color: var(--color-accent);">Total</span>
					<svg class="w-4 h-4 shrink-0" style="color: var(--color-accent); opacity: 0.7;" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>
				</div>
				<p class="text-2xl font-bold tracking-tight">{enrichedInstances.length}</p>
				<p class="text-xs mt-1 truncate" style="color: var(--color-muted);">{[region, ...extraRegions].join(', ')}</p>
			</div>
		</div>
	{/if}

	<!-- Cost / Waste panel -->
	{#if !loading && instances.length > 0 && (wasteFlags.longRunning.length > 0 || wasteFlags.stoppedInstances.length > 0 || wasteFlags.neverTagged.length > 0 || volumesLoaded)}
		<div class="rounded-xl border mb-4 p-4 text-xs" style="border-color: var(--color-warn-border); background-color: var(--color-warn-bg);">
			<div class="flex flex-wrap items-center gap-x-4 gap-y-1">
				<span class="font-semibold uppercase tracking-wide" style="color: var(--color-warn-text);">Cost &amp; Waste</span>
				{#if estimatedMonthlyCost > 0}
					<span style="color: var(--color-warn-text); font-weight: 600;">~{fmtCost(estimatedMonthlyCost)}/mo running</span>
				{/if}
				{#if wasteFlags.longRunning.length > 0}
					<button onclick={() => { filterStates = new Set(['running']); }} class="flex items-center gap-1 underline underline-offset-2" style="color: var(--color-warn-text);" title="Show these instances">
						⚠ {wasteFlags.longRunning.length} running &gt;30 days
					</button>
				{/if}
				{#if wasteFlags.stoppedInstances.length > 0}
					<button onclick={() => { filterStates = new Set(['stopped']); }} class="flex items-center gap-1" style="color: #eab308;" title="Stopped instances still incur EBS storage charges">
						⚠ {wasteFlags.stoppedInstances.length} stopped (EBS costs)
					</button>
				{/if}
				{#if wasteFlags.neverTagged.length > 0}
					<span style="color: var(--color-muted);">⚠ {wasteFlags.neverTagged.length} untagged</span>
				{/if}
				{#if volumesLoaded && unattachedVolumes.length > 0}
					<span style="color: #ef4444;">⚠ {unattachedVolumes.length} unattached volumes (~{fmtCost(unattachedEBSWaste)}/mo waste)</span>
				{/if}
				{#if !volumesLoaded}
					<button
						onclick={loadVolumes}
						disabled={volumesLoading}
						class="ml-auto text-xs rounded px-2 py-0.5 border transition-colors disabled:opacity-50"
						style="border-color: var(--color-border); color: var(--color-muted);"
					>{volumesLoading ? 'Loading…' : 'Check EBS volumes'}</button>
				{/if}
			</div>
			{#if volumesLoaded && volumes.length > 0}
				<div class="mt-3 overflow-x-auto">
					<table class="w-full text-xs">
						<thead>
							<tr style="border-bottom: 1px solid var(--color-border);">
								<th class="px-2 py-1.5 w-6"></th>
								{#each ['Volume ID','Name','State','Size','Type','Attached To','Age','Est. Cost/mo'] as h}
									<th class="text-left px-2 py-1.5 font-semibold" style="color: var(--color-muted);">{h}</th>
								{/each}
							</tr>
						</thead>
						<tbody>
							{#each volumes as v}
								{@const isWaste = v.state === 'available'}
								{@const ageDays = Math.floor((Date.now() - new Date(v.create_time).getTime()) / 86400000)}
								{@const cost = estimateEBSCostPerMonth(v.volume_type, v.size_gb, region)}
								{@const isVolExpanded = expandedVolumeId === v.volume_id}
								<tr
									class="cursor-pointer transition-colors"
									style="border-bottom: {isVolExpanded ? 'none' : '1px solid var(--color-border)'}; {isWaste ? 'background-color: #f59e0b08;' : ''}"
									onclick={() => expandedVolumeId = isVolExpanded ? null : v.volume_id}
									onmouseenter={(e) => (e.currentTarget as HTMLElement).style.backgroundColor = 'var(--color-surface)'}
									onmouseleave={(e) => (e.currentTarget as HTMLElement).style.backgroundColor = isWaste ? '#f59e0b08' : (isVolExpanded ? 'var(--color-surface)' : '')}
								>
									<td class="px-2 py-1.5 w-6 text-center">
										<svg class="w-3 h-3 inline transition-transform" style="color: var(--color-muted); transform: rotate({isVolExpanded ? '90deg' : '0deg'});" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"/></svg>
									</td>
									<td class="px-2 py-1.5 font-mono">{v.volume_id}</td>
									<td class="px-2 py-1.5" style="color: var(--color-muted);">{v.name ?? '—'}</td>
									<td class="px-2 py-1.5">
										{#if isWaste}
											<span class="font-semibold" style="color: #f59e0b;">⚠ unattached</span>
										{:else}
											<span style="color: var(--color-muted);">{v.state}</span>
										{/if}
									</td>
									<td class="px-2 py-1.5">{v.size_gb} GB</td>
									<td class="px-2 py-1.5 font-mono" style="color: var(--color-muted);">{v.volume_type}</td>
									<td class="px-2 py-1.5 font-mono" style="color: var(--color-muted);">{v.attachments.map(a => a.instance_id).join(', ') || '—'}</td>
									<td class="px-2 py-1.5" style="color: var(--color-muted);">{ageDays}d</td>
									<td class="px-2 py-1.5 font-medium" style="color: {isWaste ? '#f59e0b' : 'var(--color-muted)'};">~{fmtCost(cost)}</td>
								</tr>
								{#if isVolExpanded}
									<tr style="border-bottom: 1px solid var(--color-border); background-color: var(--color-surface);">
										<td colspan={9} class="px-4 pb-3 pt-2">
											<div class="grid grid-cols-2 gap-4 text-xs">
												<div>
													<p class="font-semibold mb-1.5" style="color: var(--color-muted);">STORAGE</p>
													<dl class="space-y-1">
														{#each [['Created', fmtDate(v.create_time)], ['Volume Type', v.volume_type], ['IOPS', v.iops != null ? String(v.iops) : '—'], ['Throughput', v.throughput != null ? v.throughput + ' MB/s' : '—']] as [label, val]}
															<div class="flex gap-2">
																<dt class="w-24 shrink-0" style="color: var(--color-muted);">{label}</dt>
																<dd class="font-mono">{val}</dd>
															</div>
														{/each}
													</dl>
												</div>
												<div>
													{#if v.attachments.length > 0}
														<p class="font-semibold mb-1.5" style="color: var(--color-muted);">ATTACHED TO</p>
														{#each v.attachments as a}
															<div class="flex items-center gap-2 mb-1.5 flex-wrap">
																<span class="font-mono">{a.instance_id}</span>
																<span style="color: var(--color-muted);">on {a.device}</span>
																<button
																	onclick={(e) => { e.stopPropagation(); jumpToInstance(a.instance_id); }}
																	class="text-xs rounded px-2 py-0.5 border transition-colors"
																	style="border-color: var(--color-accent); color: var(--color-accent);"
																>Jump to instance →</button>
															</div>
														{/each}
													{:else}
														<p class="font-semibold mb-1.5" style="color: var(--color-muted);">ATTACHED TO</p>
														<p style="color: var(--color-muted);">Not attached</p>
													{/if}
													{#if v.tags?.length}
														<p class="font-semibold mb-1.5 mt-2" style="color: var(--color-muted);">TAGS</p>
														<div class="flex flex-wrap gap-1">
															{#each v.tags as tag}
																<span class="rounded px-1.5 py-0.5 font-mono" style="background-color: var(--color-border);">{tag.Key}: <span style="color: var(--color-muted);">{tag.Value}</span></span>
															{/each}
														</div>
													{/if}
												</div>
											</div>
										</td>
									</tr>
								{/if}
							{/each}
						</tbody>
					</table>
				</div>
			{/if}
		</div>
	{/if}

	<div class="rounded-xl border overflow-hidden" style="border-color: var(--color-border);">
		<div class="overflow-x-auto max-h-[calc(100vh-300px)] overflow-y-auto">
			<table class="w-full text-sm">
				<thead class="sticky top-0 z-10">
					<tr style="background-color: var(--color-surface); border-bottom: 2px solid var(--color-border);">
						<th class="px-2 py-3 w-8"></th>
						{#each ALL_COLUMNS.filter(c => effectiveVisibleCols.has(c.key)) as col}
							<th
								class="text-left px-4 py-3 font-semibold whitespace-nowrap {col.sort ? 'cursor-pointer select-none' : ''}"
								style="color: var(--color-muted);"
								onclick={() => col.sort && toggleSort(col.sort)}
							>
								{col.label}
								{#if col.evt}
									{#if eventsLoading}<svg class="inline w-3 h-3 animate-spin ml-1" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 11-6.219-8.56"/></svg>{/if}
								{/if}
								{#if col.sort && sortCol === col.sort}
									<svg class="inline w-3 h-3 ml-0.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
										{#if sortDir === 'asc'}<polyline points="18 15 12 9 6 15"/>{:else}<polyline points="6 9 12 15 18 9"/>{/if}
									</svg>
								{/if}
							</th>
						{/each}
					</tr>
				</thead>
				<tbody>
					{#if loading}
						{#each { length: 5 } as _}
							<tr style="border-bottom: 1px solid var(--color-border);">
								<td class="px-2 py-3 w-8"></td>
								{#each { length: effectiveVisibleCols.size } as _}
									<td class="px-4 py-3"><div class="h-4 rounded animate-pulse w-24" style="background-color: var(--color-border);"></div></td>
								{/each}
							</tr>
						{/each}
					{:else if instances.length === 0 && !error}
						<tr>
							<td colspan={tableColspan} class="px-4 py-12 text-center" style="color: var(--color-muted);">No instances found in {region}</td>
						</tr>
					{:else if sortedInstances.length === 0 && hasActiveFilters}
						<tr>
							<td colspan={tableColspan} class="px-4 py-12 text-center" style="color: var(--color-muted);">
								No instances match the current filters.
								<button onclick={clearFilters} class="ml-2 underline" style="color: var(--color-accent);">Clear filters</button>
							</td>
						</tr>
					{:else}
						{#each sortedInstances as inst (inst.instance_id)}
							{@const isExpanded = expandedId === inst.instance_id}
							{@const isActing = actionLoading === inst.instance_id}
							<tr
								class="transition-colors cursor-pointer"
								style="border-bottom: {isExpanded ? 'none' : '1px solid var(--color-border)'};"
								onclick={() => { expandedId = isExpanded ? null : inst.instance_id; confirmTerminateId = null; confirmInput = ''; editingTagsId = null; }}
								onmouseenter={(e) => (e.currentTarget as HTMLElement).style.backgroundColor = 'var(--color-surface)'}
								onmouseleave={(e) => (e.currentTarget as HTMLElement).style.backgroundColor = isExpanded ? 'var(--color-surface)' : ''}
							>
								<td class="px-2 py-3 w-8 text-center">
									<svg class="w-3.5 h-3.5 inline transition-transform" style="color: var(--color-muted); transform: rotate({isExpanded ? '90deg' : '0deg'});" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"/></svg>
								</td>
								{#if effectiveVisibleCols.has('name')}<td class="px-4 py-3 font-medium">{inst.name}</td>{/if}
								{#if effectiveVisibleCols.has('region')}<td class="px-4 py-3 font-mono text-xs" style="color: var(--color-muted);">{inst.region ?? region}</td>{/if}
								{#if effectiveVisibleCols.has('id')}
								<td class="px-4 py-3 font-mono text-xs group/iid" style="color: var(--color-muted);">
									{inst.instance_id}
									<button
										onclick={(e) => copyToClipboard(inst.instance_id, e)}
										class="ml-1 opacity-0 group-hover/iid:opacity-100 transition-opacity"
										title={copiedValue === inst.instance_id ? 'Copied!' : 'Copy ID'}
										style="color: {copiedValue === inst.instance_id ? '#22c55e' : 'var(--color-muted)'};"
									>
										{#if copiedValue === inst.instance_id}
											<svg class="inline w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>
										{:else}
											<svg class="inline w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
										{/if}
									</button>
								</td>
								{/if}
								{#if effectiveVisibleCols.has('state')}
								<td class="px-4 py-3">
									<span class="inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs font-medium uppercase tracking-wide"
										style="background-color: {stateColor(inst.state)}22; color: {stateColor(inst.state)};">
										<span class="w-1.5 h-1.5 rounded-full" style="background-color: {stateColor(inst.state)};"></span>
										{inst.state}
									</span>
								</td>
								{/if}
								{#if effectiveVisibleCols.has('type')}<td class="px-4 py-3" style="color: var(--color-muted);">{inst.instance_type}</td>{/if}
								{#if effectiveVisibleCols.has('az')}<td class="px-4 py-3" style="color: var(--color-muted);">{inst.availability_zone}</td>{/if}
								{#if effectiveVisibleCols.has('launch')}<td class="px-4 py-3 whitespace-nowrap" title={inst.launch_time}>{fmtDate(inst.launch_time)}</td>{/if}
								{#if effectiveVisibleCols.has('started')}
								<td class="px-4 py-3 whitespace-nowrap">
									{#if eventsLoading && !inst.first_started}
										<span style="color: var(--color-muted);">···</span>
									{:else if inst.first_started}
										<span title={inst.first_started}>{fmtDate(inst.first_started)}</span>
									{:else}
										<span title="No RunInstances event found — instance may pre-date the 90-day CloudTrail retention window" style="color: var(--color-muted);" class="cursor-help">—</span>
									{/if}
								</td>
								{/if}
								{#if effectiveVisibleCols.has('username')}
								<td class="px-4 py-3">
									{#if eventsLoading && !inst.username}
										<span style="color: var(--color-muted);">···</span>
									{:else if inst.username}
										<span class="font-mono text-xs" title={inst.username}>{shortUsername(inst.username)}</span>
									{:else}
										<span title="No RunInstances event found — instance may pre-date the 90-day CloudTrail retention window" style="color: var(--color-muted);" class="cursor-help">—</span>
									{/if}
								</td>
								{/if}
								{#if effectiveVisibleCols.has('stopped')}
								<td class="px-4 py-3 whitespace-nowrap">
									{#if eventsLoading && !inst.last_stopped}
										<span style="color: var(--color-muted);">···</span>
									{:else if inst.last_stopped}
										<span title={inst.last_stopped}>{fmtDate(inst.last_stopped)}</span>
									{:else}
										<span title="No StopInstances event found in CloudTrail window" style="color: var(--color-muted);" class="cursor-help">—</span>
									{/if}
								</td>
								{/if}
								{#if effectiveVisibleCols.has('private_ip')}<td class="px-4 py-3 font-mono text-xs" style="color: var(--color-muted);">{inst.private_ip ?? '—'}</td>{/if}
								{#if effectiveVisibleCols.has('public_ip')}<td class="px-4 py-3 font-mono text-xs" style="color: var(--color-muted);">{inst.public_ip ?? '—'}</td>{/if}
								{#if effectiveVisibleCols.has('vpc')}<td class="px-4 py-3 font-mono text-xs" style="color: var(--color-muted);">{inst.vpc_id ?? '—'}</td>{/if}
								{#if effectiveVisibleCols.has('iam')}<td class="px-4 py-3 font-mono text-xs truncate max-w-xs" style="color: var(--color-muted);" title={inst.iam_profile ?? ''}>{inst.iam_profile ? shortUsername(inst.iam_profile) : '—'}</td>{/if}
							{#if effectiveVisibleCols.has('cost')}
							{@const instRegion = inst.region ?? region}
							{@const ltCost = instanceLifetimeCost(inst)}
							{@const mr = monthlyRate(inst.instance_type, instRegion)}
							<td class="px-4 py-3 text-xs whitespace-nowrap" style="color: var(--color-muted);">
								{#if inst.state === 'running'}
									<span title="{pricingSource(instRegion) === 'live' ? 'Live on-demand Linux price' : 'Estimated on-demand Linux price'}, {instRegion}">{pricingSource(instRegion) === 'estimate' ? '~' : ''}{fmtCost(mr ?? 0)}/mo</span>
								{:else if inst.state === 'stopped'}
									<span style="color: #eab308;" title="Stopped: no compute cost but EBS storage still billed">EBS only</span>
								{:else}
									<span>—</span>
								{/if}
								{#if ltCost != null && ltCost > 0}
									<div class="mt-0.5 text-xs" style="color: var(--color-muted);" title="Estimated total compute cost since launch">~{fmtCost(ltCost)} total</div>
								{/if}
							</td>
							{/if}
							{#if effectiveVisibleCols.has('compliance')}
							<td class="px-4 py-3 text-xs">
								{#if requiredTags.length === 0}
									<span style="color: var(--color-muted);">—</span>
								{:else}
									{@const missing = missingTags(inst)}
									{#if missing.length === 0}
										<span style="color: #22c55e;" title="All required tags present">✓</span>
									{:else}
										<span style="color: #ef4444;" title="Missing: {missing.join(', ')}">✗ {missing.length}</span>
									{/if}
								{/if}
							</td>
							{/if}
							</tr>

							{#if isExpanded}
								<tr style="border-bottom: 1px solid var(--color-border); background-color: var(--color-surface);">
									<td colspan={tableColspan} class="px-4 pb-4 pt-3">

										<!-- Network + instance metadata grid -->
										<div class="grid grid-cols-2 gap-4 mb-4 text-xs">
											<div>
												<p class="text-xs font-semibold uppercase tracking-widest mb-2" style="color: var(--color-muted);">NETWORK</p>
												<dl class="space-y-1">
													{#each [['Private IP', inst.private_ip], ['Public IP', inst.public_ip], ['VPC', inst.vpc_id], ['Subnet', inst.subnet_id]] as [label, val]}
														<div class="flex gap-2 group/copyfield">
															<dt class="w-24 shrink-0" style="color: var(--color-muted);">{label}</dt>
															<dd class="font-mono flex items-center gap-1">
																{val ?? '—'}
																{#if val}
																	<button onclick={(e) => copyToClipboard(val, e)} class="opacity-0 group-hover/copyfield:opacity-100 transition-opacity" title={copiedValue === val ? 'Copied!' : 'Copy'} style="color: {copiedValue === val ? '#22c55e' : 'var(--color-muted)'};">
																		{#if copiedValue === val}<svg class="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>{:else}<svg class="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>{/if}
																	</button>
																{/if}
															</dd>
														</div>
													{/each}
													{#if inst.security_groups?.length}
														<div class="flex gap-2">
															<dt class="w-24 shrink-0" style="color: var(--color-muted);">Sec Groups</dt>
															<dd class="flex flex-wrap gap-1">
																{#each inst.security_groups as sg}
																	<span class="rounded px-1.5 py-0.5 font-mono text-xs" style="background-color: var(--color-border);">{sg.name} <span style="color: var(--color-muted);">({sg.id})</span></span>
																{/each}
															</dd>
														</div>
													{/if}
												</dl>
											</div>
											<div>
												<p class="text-xs font-semibold uppercase tracking-widest mb-2" style="color: var(--color-muted);">INSTANCE</p>
												<dl class="space-y-1">
													{#each [['AMI', inst.image_id], ['Key Pair', inst.key_name], ['Architecture', inst.architecture]] as [label, val]}
														<div class="flex gap-2">
															<dt class="w-28 shrink-0" style="color: var(--color-muted);">{label}</dt>
															<dd class="font-mono">{val ?? '—'}</dd>
														</div>
													{/each}
													{#if inst.iam_profile}
														<div class="flex gap-2">
															<dt class="w-28 shrink-0" style="color: var(--color-muted);">IAM Profile</dt>
															<dd class="font-mono text-xs truncate max-w-xs" title={inst.iam_profile}>{shortUsername(inst.iam_profile)}</dd>
														</div>
													{/if}
												</dl>
											</div>
										</div>

										<!-- Lifetime Cost -->
										{#if instanceLifetimeCost(inst) != null}
										{@const instRegion = inst.region ?? region}
										<div class="border-t pt-3 mb-4" style="border-color: var(--color-border);">
											<p class="text-xs font-semibold uppercase tracking-widest mb-2" style="color: var(--color-muted);">COST</p>
											<div class="flex items-end gap-6 text-xs">
												<div>
													<p class="text-xl font-bold tracking-tight">{pricingSource(inst.region ?? region) === 'estimate' ? '~' : ''}{fmtCost(instanceLifetimeCost(inst)!)}</p>
													<p class="mt-0.5" style="color: var(--color-muted);">{pricingSource(inst.region ?? region) === 'live' ? '' : 'estimated '}lifetime total</p>
												</div>
												{#if monthlyRate(inst.instance_type, instRegion) != null}
												<div style="color: var(--color-muted);">
													<p class="font-medium" style="color: var(--color-text-secondary);">{pricingSource(instRegion) === 'estimate' ? '~' : ''}{fmtCost(monthlyRate(inst.instance_type, instRegion)!)}/mo</p>
													<p class="mt-0.5">on-demand rate</p>
												</div>
												{/if}
												<div style="color: var(--color-muted);">
													{#if (Date.now() - new Date(inst.launch_time).getTime()) / 3_600_000 < 24}
														<p class="font-medium" style="color: var(--color-text-secondary);">{Math.round((Date.now() - new Date(inst.launch_time).getTime()) / 3_600_000)}h</p>
													{:else if (Date.now() - new Date(inst.launch_time).getTime()) / 3_600_000 < 24 * 30}
														<p class="font-medium" style="color: var(--color-text-secondary);">{Math.round((Date.now() - new Date(inst.launch_time).getTime()) / 86_400_000)}d</p>
													{:else}
														<p class="font-medium" style="color: var(--color-text-secondary);">{Math.round((Date.now() - new Date(inst.launch_time).getTime()) / (86_400_000 * 30))}mo</p>
													{/if}
													<p class="mt-0.5">since launch</p>
												</div>
											</div>
											<p class="mt-2 text-xs" style="color: var(--color-muted);">
												{pricingSource(instRegion) === 'live' ? 'Live' : 'Estimated'} on-demand Linux · {instRegion}
											</p>
										</div>
										{/if}

										<!-- Event History -->
										<div class="border-t pt-3 mb-4" style="border-color: var(--color-border);">
											<p class="text-xs font-semibold mb-2" style="color: var(--color-muted);">EVENT HISTORY</p>
											<dl class="space-y-1 text-xs">
												{#each [
													{ label: 'First Launched', event: 'RunInstances',       value: inst.first_started,    by: inst.username },
													{ label: 'Last Started',   event: 'StartInstances',    value: inst.last_started,     by: inst.last_started_by },
													{ label: 'Last Stopped',   event: 'StopInstances',     value: inst.last_stopped,     by: inst.last_stopped_by },
													{ label: 'Terminated',     event: 'TerminateInstances',value: inst.last_terminated,  by: inst.last_terminated_by },
												] as row}
													<div class="flex gap-2 items-center">
														<span class="w-1.5 h-1.5 rounded-full shrink-0" style="background-color: {eventColor(row.event)};"></span>
														<dt class="w-28 shrink-0" style="color: var(--color-muted);">{row.label}</dt>
														<dd>
															{#if eventsLoading && !row.value}
																<span style="color: var(--color-muted);">···</span>
															{:else if row.value}
																<span title={row.value}>{fmtDate(row.value)}</span>
																{#if row.by}
																	<span class="ml-1" style="color: var(--color-muted);">by {shortUsername(row.by)}</span>
																{/if}
															{:else}
																<span title="No event found in CloudTrail window" style="color: var(--color-muted);">—</span>
															{/if}
														</dd>
													</div>
												{/each}
											</dl>
										</div>

										<!-- Tags -->
										<div class="border-t pt-3 mb-4" style="border-color: var(--color-border);">
											<div class="flex items-center gap-2 mb-2">
												<p class="text-xs font-semibold" style="color: var(--color-muted);">TAGS</p>
												{#if editingTagsId !== inst.instance_id}
													<button onclick={(e) => { e.stopPropagation(); startTagEdit(inst); }}
														class="text-xs rounded px-2 py-0.5 border transition-colors"
														style="border-color: var(--color-border); color: var(--color-muted);">Edit tags</button>
												{/if}
											</div>

											{#if editingTagsId === inst.instance_id}
												<div onclick={(e) => e.stopPropagation()} role="presentation">
													{#each draftTags as tag, i}
														<div class="flex gap-2 mb-1.5 items-center">
															<input type="text" bind:value={tag.Key} placeholder="Key"
																class="rounded px-2 py-1 text-xs border w-36 font-mono"
																style="background-color: var(--color-bg); border-color: var(--color-border); color: var(--color-text);" />
															<input type="text" bind:value={tag.Value} placeholder="Value"
																class="rounded px-2 py-1 text-xs border flex-1 font-mono"
																style="background-color: var(--color-bg); border-color: var(--color-border); color: var(--color-text);" />
															<button onclick={() => draftTags = draftTags.filter((_, j) => j !== i)}
																class="text-xs rounded px-1.5 py-1" style="color: #f87171;">✕</button>
														</div>
													{/each}
													<div class="flex gap-2 mt-2">
														<button onclick={() => draftTags = [...draftTags, { Key: '', Value: '' }]}
															class="text-xs rounded px-2 py-1 border transition-colors"
															style="border-color: var(--color-border); color: var(--color-muted);">+ Add tag</button>
														<button onclick={() => saveTagEdit(inst.instance_id, inst.tags)}
															disabled={isActing}
															class="text-xs rounded px-2 py-1 transition-colors disabled:opacity-40"
															style="background-color: var(--color-accent); color: white;">
															{isActing ? 'Saving…' : 'Save'}
														</button>
														<button onclick={() => editingTagsId = null}
															class="text-xs rounded px-2 py-1 border transition-colors"
															style="border-color: var(--color-border); color: var(--color-muted);">Cancel</button>
													</div>
												</div>
											{:else}
												<div class="flex flex-wrap gap-1">
													{#each (inst.tags ?? []) as tag}
														<span class="rounded px-2 py-0.5 text-xs font-mono" style="background-color: var(--color-border);">
															{tag.Key}: <span style="color: var(--color-muted);">{tag.Value}</span>
														</span>
													{/each}
													{#if !(inst.tags?.length)}
														<span class="text-xs" style="color: var(--color-muted);">No tags</span>
													{/if}
												</div>
											{/if}
										</div>

										<!-- Actions -->
										<div class="border-t pt-3" style="border-color: var(--color-border);">
											{#if actionError[inst.instance_id]}
												<p class="text-xs mb-2 rounded px-2 py-1" style="background-color: #1f0a0a; color: #fca5a5;">{actionError[inst.instance_id]}</p>
											{/if}

											{#if confirmTerminateId === inst.instance_id}
												<div class="flex items-center gap-2" onclick={(e) => e.stopPropagation()} role="presentation">
													<span class="text-xs" style="color: var(--color-muted);">Type <code class="font-mono" style="color: #f87171;">{inst.instance_id}</code> to confirm:</span>
													<input type="text" bind:value={confirmInput} placeholder={inst.instance_id}
														class="rounded px-2 py-1 text-xs border font-mono w-52"
														style="background-color: var(--color-bg); border-color: #7f1d1d; color: var(--color-text);" />
													<button
														onclick={() => doAction(inst.instance_id, 'terminate')}
														disabled={confirmInput !== inst.instance_id || isActing}
														class="text-xs rounded px-3 py-1 font-medium transition-colors disabled:opacity-40"
														style="background-color: #7f1d1d; color: #fca5a5;">
														{isActing ? 'Terminating…' : 'Terminate'}
													</button>
													<button onclick={(e) => { e.stopPropagation(); confirmTerminateId = null; confirmInput = ''; }}
														class="text-xs rounded px-2 py-1 border transition-colors"
														style="border-color: var(--color-border); color: var(--color-muted);">Cancel</button>
												</div>
											{:else}
												<div class="flex gap-2" onclick={(e) => e.stopPropagation()} role="presentation">
													<button
														onclick={() => doAction(inst.instance_id, 'start')}
														disabled={inst.state !== 'stopped' || isActing}
														class="flex items-center gap-1.5 text-xs rounded px-3 py-1.5 font-medium border transition-colors disabled:opacity-30"
														style="border-color: #166534; color: #86efac;">
														{#if isActing && actionLoading === inst.instance_id}<svg class="w-3 h-3 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 11-6.219-8.56"/></svg>{/if}
														▶ Start
													</button>
													<button
														onclick={() => doAction(inst.instance_id, 'stop')}
														disabled={inst.state !== 'running' || isActing}
														class="flex items-center gap-1.5 text-xs rounded px-3 py-1.5 font-medium border transition-colors disabled:opacity-30"
														style="border-color: #713f12; color: #fde68a;">
														■ Stop
													</button>
													<button
														onclick={() => doAction(inst.instance_id, 'reboot')}
														disabled={inst.state !== 'running' || isActing}
														class="flex items-center gap-1.5 text-xs rounded px-3 py-1.5 font-medium border transition-colors disabled:opacity-30"
														style="border-color: #1e3a5f; color: #93c5fd;">
														↺ Reboot
													</button>
													<button
														onclick={(e) => { e.stopPropagation(); confirmTerminateId = inst.instance_id; confirmInput = ''; }}
														disabled={inst.state === 'terminated' || isActing}
														class="flex items-center gap-1.5 text-xs rounded px-3 py-1.5 font-medium border transition-colors disabled:opacity-30"
														style="border-color: #7f1d1d; color: #fca5a5;">
														✕ Terminate
													</button>
												</div>
											{/if}
										</div>

									</td>
								</tr>
							{/if}
						{/each}
					{/if}
				</tbody>
			</table>
		</div>
	</div>

<!-- ── TIMELINE TAB ── -->
{:else if activeTab === 'timeline'}
	{#if importError}
		<div class="mb-4 rounded-xl border px-4 py-3 text-sm" style="background-color: var(--color-error-bg); border-color: var(--color-error-border); color: var(--color-error-text);">
			<strong>Import error:</strong> {importError}
		</div>
	{/if}

	{#if eventsError && importSource === 'api'}
		<div class="mb-4 rounded-xl border px-4 py-3 text-sm" style="background-color: var(--color-error-bg); border-color: var(--color-error-border); color: var(--color-error-text);">
			<strong>AWS Error:</strong> {eventsError}
		</div>
	{/if}

	<!-- Timeline filters -->
	{#if activeEvents.length > 0 || tlFilterFrom || tlFilterTo || tlFilterEvents.size > 0 || tlFilterUser || tlFilterInstanceId}
		<div class="flex flex-wrap items-center gap-2 mb-4">
			{#if tlFilterInstanceId}
				<span class="flex items-center gap-1.5 rounded px-2.5 py-1 text-xs font-medium" style="background-color: #6366f122; color: #a5b4fc; border: 1px solid #6366f144;">
					Instance: {instanceNameMap[tlFilterInstanceId] ?? tlFilterInstanceId}
					<button onclick={() => tlFilterInstanceId = ''} class="opacity-70 hover:opacity-100">✕</button>
				</span>
			{/if}
			<div class="flex gap-1">
				{#each [['RunInstances','Launched','#6366f1'],['StartInstances','Started','#22c55e'],['StopInstances','Stopped','#eab308'],['TerminateInstances','Terminated','#ef4444']] as [name, label, color]}
					<button
						onclick={() => toggleTlEventFilter(name)}
						class="flex items-center gap-1 rounded-full px-2.5 py-1 text-xs font-medium transition-colors"
						style="background-color: {tlFilterEvents.has(name) ? color + '33' : 'var(--color-surface)'}; border: 1px solid {tlFilterEvents.has(name) ? color : 'var(--color-border)'}; color: {tlFilterEvents.has(name) ? color : 'var(--color-muted)'};"
					>
						<span class="w-1.5 h-1.5 rounded-full" style="background-color: {color};"></span>
						{label}
					</button>
				{/each}
			</div>
			<input type="date" bind:value={tlFilterFrom} class="rounded px-2 py-1.5 text-xs border" style="background-color: var(--color-surface); border-color: var(--color-border); color: var(--color-text);" title="From date" />
			<input type="date" bind:value={tlFilterTo} class="rounded px-2 py-1.5 text-xs border" style="background-color: var(--color-surface); border-color: var(--color-border); color: var(--color-text);" title="To date" />
			<input type="text" bind:value={tlFilterUser} placeholder="Filter user…" class="rounded px-2.5 py-1.5 text-xs border w-40" style="background-color: var(--color-surface); border-color: var(--color-border); color: var(--color-text);" />
			{#if tlFilterFrom || tlFilterTo || tlFilterEvents.size > 0 || tlFilterUser || tlFilterInstanceId}
				<button onclick={() => { tlFilterFrom = ''; tlFilterTo = ''; tlFilterEvents = new Set(); tlFilterUser = ''; tlFilterInstanceId = ''; }} class="text-xs rounded px-2.5 py-1.5 border transition-colors" style="border-color: var(--color-border); color: var(--color-muted);">Clear all</button>
				<span class="text-xs ml-1" style="color: var(--color-muted);">{filteredActiveEvents.length} of {activeEvents.length} events</span>
			{/if}
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
										<span class="inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs font-medium uppercase tracking-wide"
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
<!-- ── LIFETIME TAB ── -->
{:else if activeTab === 'lifetime'}
	<!-- Zoom controls -->
	<div class="flex flex-wrap items-center gap-3 mb-4">
		<span class="text-xs font-semibold" style="color: var(--color-muted);">ZOOM</span>
		<input type="date" bind:value={ganttZoomFrom} class="rounded px-2 py-1.5 text-xs border" style="background-color: var(--color-surface); border-color: var(--color-border); color: var(--color-text);" title="Zoom from" />
		<span class="text-xs" style="color: var(--color-muted);">→</span>
		<input type="date" bind:value={ganttZoomTo} class="rounded px-2 py-1.5 text-xs border" style="background-color: var(--color-surface); border-color: var(--color-border); color: var(--color-text);" title="Zoom to" />
		{#if ganttZoomFrom || ganttZoomTo}
			<button onclick={() => { ganttZoomFrom = ''; ganttZoomTo = ''; }} class="text-xs rounded px-2.5 py-1.5 border transition-colors" style="border-color: var(--color-border); color: var(--color-muted);">Reset zoom</button>
		{/if}
		<div class="flex items-center gap-3 ml-auto text-xs" style="color: var(--color-muted);">
			<span><span class="inline-block w-3 h-3 rounded-sm mr-1" style="background-color: #22c55e88;"></span>running</span>
			<span><span class="inline-block w-3 h-3 rounded-sm mr-1" style="background-color: #eab30888;"></span>stopped</span>
			<span><span class="inline-block w-3 h-3 rounded-sm mr-1" style="background-color: #ef444488;"></span>terminated</span>
			<span style="color: var(--color-muted);">┆ dashed = origin unknown</span>
		</div>
	</div>

	{#if ganttRows.length === 0 && !eventsLoading}
		<div class="rounded-lg border p-10 text-center" style="border-color: var(--color-border); border-style: dashed;">
			<svg class="w-8 h-8 mx-auto mb-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" style="color: var(--color-muted);">
				<path d="M9 19v-6a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2h2a2 2 0 0 0 2-2zm0 0V9a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v10m-6 0a2 2 0 0 0 2 2h2a2 2 0 0 0 2-2m0 0V5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-2a2 2 0 0 1-2-2z"/>
			</svg>
			<p class="text-sm font-medium mb-1">No lifetime data yet</p>
			<p class="text-xs mb-4" style="color: var(--color-muted);">Load CloudTrail events to see instance lifecycles, or instances will appear from their launch time.</p>
			{#if !eventsLoaded}
				<button onclick={loadEventsData} disabled={eventsLoading} class="inline-flex items-center gap-2 rounded px-4 py-2 text-sm font-medium disabled:opacity-50" style="background-color: var(--color-accent); color: white;">
					Load CloudTrail Events
				</button>
			{/if}
		</div>
	{:else if eventsLoading && ganttRows.length === 0}
		<div class="flex items-center gap-3 py-8 text-sm" style="color: var(--color-muted);">
			<svg class="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 11-6.219-8.56"/></svg>
			Loading events…
		</div>
	{:else}
		<!-- Gantt chart -->
		<div class="rounded-lg border overflow-hidden relative" style="border-color: var(--color-border);">
			<div class="flex" style="min-height: {GANTT_HDR_H + ganttRows.length * GANTT_ROW_H}px;">

				<!-- Label column (fixed, non-scrolling) -->
				<div class="shrink-0 border-r" style="width: 224px; border-color: var(--color-border); background-color: var(--color-surface);">
					<div class="border-b flex items-end px-3 pb-1" style="height: {GANTT_HDR_H}px; border-color: var(--color-border);">
						<span class="text-xs font-semibold" style="color: var(--color-muted);">INSTANCE</span>
					</div>
					{#each ganttRows as row}
						<div
							role="button"
							tabindex="0"
							class="flex items-center gap-1.5 px-3 border-b cursor-pointer transition-colors"
							style="height: {GANTT_ROW_H}px; border-color: var(--color-border);"
							title="Click to view events for this instance"
							onclick={() => jumpToTimeline(row.instanceId)}
							onkeydown={(e) => e.key === 'Enter' && jumpToTimeline(row.instanceId)}
							onmouseenter={(e) => (e.currentTarget as HTMLElement).style.backgroundColor = 'var(--color-border)'}
							onmouseleave={(e) => (e.currentTarget as HTMLElement).style.backgroundColor = ''}
						>
							<span class="text-xs font-medium truncate flex-1">{row.name}</span>
							<span class="rounded-full px-1.5 py-0.5 shrink-0" style="font-size: 9px; background-color: {stateColor(row.currentState)}22; color: {stateColor(row.currentState)};">{row.currentState}</span>
						</div>
					{/each}
				</div>

				<!-- Scrollable chart area -->
				<div bind:this={ganttScrollEl} class="flex-1 overflow-x-auto">
					<svg
						width={ganttSvgWidth}
						height={GANTT_HDR_H + ganttRows.length * GANTT_ROW_H}
						style="display: block;"
					>
						<!-- Time axis ticks and grid lines -->
						{#each ganttTicks(ganttViewRange.min, ganttViewRange.max, ganttSvgWidth) as tick}
							<line x1={tick.x} y1={0} x2={tick.x} y2={GANTT_HDR_H + ganttRows.length * GANTT_ROW_H} stroke="var(--color-border)" stroke-width="1" />
							<text x={tick.x + 4} y={GANTT_HDR_H - 8} font-size="10" fill="var(--color-muted)">{tick.label}</text>
						{/each}
						<line x1={0} y1={GANTT_HDR_H} x2={ganttSvgWidth} y2={GANTT_HDR_H} stroke="var(--color-border)" stroke-width="1" />

						<!-- Rows -->
						{#each ganttRows as row, i}
							{@const yRow = GANTT_HDR_H + i * GANTT_ROW_H}
							{@const yBar = yRow + (GANTT_ROW_H - GANTT_BAR_H) / 2}
							{@const range = ganttViewRange.max - ganttViewRange.min}

							<!-- Row separator -->
							<line x1={0} y1={yRow + GANTT_ROW_H} x2={ganttSvgWidth} y2={yRow + GANTT_ROW_H} stroke="var(--color-border)" stroke-width="1" opacity="0.4" />

							<!-- Segments -->
							{#each row.segments as seg}
								{@const x1 = Math.max(0, (seg.start - ganttViewRange.min) / range * ganttSvgWidth)}
								{@const x2raw = Math.min(ganttSvgWidth, (seg.end - ganttViewRange.min) / range * ganttSvgWidth)}
								{@const barW = seg.state === 'terminated' ? 8 : Math.max(x2raw - x1, 0)}
								{#if barW > 0 && x1 <= ganttSvgWidth}
									<rect
										role="button"
										tabindex="0"
										x={x1} y={yBar}
										width={barW} height={GANTT_BAR_H}
										rx="3" ry="3"
										fill={stateColor(seg.state)}
										opacity={seg.dashed ? 0.5 : 0.75}
										style="cursor: pointer;"
										onmouseenter={(e: MouseEvent) => ganttTooltip = { x: e.clientX, y: e.clientY, row, seg }}
										onmousemove={(e: MouseEvent) => { if (ganttTooltip) ganttTooltip = { ...ganttTooltip, x: e.clientX, y: e.clientY }; }}
										onmouseleave={() => ganttTooltip = null}
										onclick={() => jumpToTimeline(row.instanceId)}
										onkeydown={(e: KeyboardEvent) => e.key === 'Enter' && jumpToTimeline(row.instanceId)}
									/>
									{#if seg.dashed}
										<line x1={x1} y1={yBar} x2={x1} y2={yBar + GANTT_BAR_H} stroke="white" stroke-dasharray="2 2" stroke-width="2" opacity="0.7" />
									{/if}
								{/if}
							{/each}
						{/each}

						<!-- "Now" marker -->
						{#if Date.now() > ganttViewRange.min && Date.now() < ganttViewRange.max}
							{@const nowX = (Date.now() - ganttViewRange.min) / (ganttViewRange.max - ganttViewRange.min) * ganttSvgWidth}
							<line x1={nowX} y1={0} x2={nowX} y2={GANTT_HDR_H + ganttRows.length * GANTT_ROW_H} stroke="#ef4444" stroke-dasharray="4 3" stroke-width="1.5" opacity="0.65" />
							<text x={nowX + 3} y={14} font-size="9" fill="#ef4444" font-weight="600">now</text>
						{/if}
					</svg>
				</div>
			</div>

			<!-- Summary footer -->
			<div class="px-4 py-2 border-t text-xs flex items-center gap-3" style="background-color: var(--color-surface); border-color: var(--color-border); color: var(--color-muted);">
				<span>{ganttRows.length} instance{ganttRows.length !== 1 ? 's' : ''}</span>
				<span>·</span>
				<span>{new Date(ganttViewRange.min).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })} → {new Date(ganttViewRange.max).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })}</span>
				<span class="ml-auto">Click a bar or row label to view events in Timeline</span>
			</div>
		</div>
	{/if}

	<!-- Tooltip -->
	{#if ganttTooltip}
		<div
			class="fixed z-[60] pointer-events-none rounded-lg border shadow-2xl px-3 py-2 text-xs"
			style="left: {ganttTooltip.x + 14}px; top: {ganttTooltip.y - 70}px; background-color: var(--color-surface); border-color: var(--color-border); min-width: 180px;"
		>
			<p class="font-semibold mb-1 truncate">{ganttTooltip.row.name}</p>
			<p class="flex items-center gap-1.5 mb-1">
				<span class="w-2 h-2 rounded-full" style="background-color: {stateColor(ganttTooltip.seg.state)};"></span>
				<span style="color: {stateColor(ganttTooltip.seg.state)};">{ganttTooltip.seg.state}</span>
			</p>
			{#if ganttTooltip.seg.state === 'terminated'}
				<p style="color: var(--color-muted);">{fmtDate(new Date(ganttTooltip.seg.start).toISOString())}</p>
			{:else}
				<p style="color: var(--color-muted);">{fmtDate(new Date(ganttTooltip.seg.start).toISOString())}</p>
				<p style="color: var(--color-muted);">→ {fmtDate(new Date(ganttTooltip.seg.end).toISOString())}</p>
				<p class="mt-1 font-medium">{fmtDuration2(ganttTooltip.seg.end - ganttTooltip.seg.start)}</p>
				{#if ganttTooltip.seg.dashed}
					<p class="mt-1 italic" style="color: var(--color-muted); font-size: 9px;">Origin pre-dates event data</p>
				{/if}
			{/if}
		</div>
	{/if}
{:else if activeTab === 'resources'}
	<!-- Mode toggle -->
	<div class="flex gap-0 mb-4 border rounded-lg overflow-hidden w-fit text-sm" style="border-color: var(--color-border);">
		<button
			onclick={() => costInventoryMode = false}
			class="px-4 py-1.5 font-medium transition-colors"
			style="background-color: {!costInventoryMode ? 'var(--color-accent)' : 'transparent'}; color: {!costInventoryMode ? 'white' : 'var(--color-muted)'};"
		>Tag Search</button>
		<button
			onclick={() => costInventoryMode = true}
			class="px-4 py-1.5 font-medium transition-colors"
			style="background-color: {costInventoryMode ? 'var(--color-accent)' : 'transparent'}; color: {costInventoryMode ? 'white' : 'var(--color-muted)'};"
		>Cost Inventory</button>
	</div>

	{#if costInventoryMode}
	<!-- Cost Inventory -->
	<div>
		<div class="flex flex-wrap items-center gap-2 mb-3">
			<button
				onclick={loadCostInventory}
				disabled={costResourcesLoading}
				class="flex items-center gap-1.5 rounded px-3 py-1.5 text-sm font-medium transition-colors disabled:opacity-50"
				style="background-color: var(--color-accent); color: white;"
			>
				{#if costResourcesLoading}
					<svg class="w-3.5 h-3.5 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 11-6.219-8.56"/></svg>
					Loading…
				{:else}
					{costResourcesLoaded ? 'Reload' : 'Load Cost Inventory'}
				{/if}
			</button>
			{#if costResourcesLoaded}
				<select bind:value={costFilterService} class="rounded px-2 py-1.5 text-sm border" style="background-color: var(--color-surface); border-color: var(--color-border); color: var(--color-text);">
					<option value="">All services</option>
					{#each distinctResourceTypes as t}
						{@const meta = resourceTypeMeta(t)}
						<option value={t}>{meta.label}</option>
					{/each}
				</select>
				<select bind:value={costSortCol} class="rounded px-2 py-1.5 text-sm border" style="background-color: var(--color-surface); border-color: var(--color-border); color: var(--color-text);">
					<option value="cost">Sort: Est. Cost</option>
					<option value="name">Sort: Name</option>
					<option value="type">Sort: Service</option>
				</select>
			{/if}
		</div>

		{#if costResourcesError}
			<div class="mb-3 rounded border px-4 py-3 text-sm" style="background-color: var(--color-error-bg); border-color: var(--color-error-border); color: var(--color-error-text);">{costResourcesError}</div>
		{/if}

		{#if costResourcesLoaded && filteredCostResources.length > 0}
			<!-- Total cost banner -->
			<div class="mb-3 rounded-lg px-4 py-2.5 text-sm flex items-center gap-3 flex-wrap" style="background-color: var(--color-surface); border: 1px solid var(--color-border);">
				<span class="font-semibold">Estimated total</span>
				<span class="text-lg font-bold" style="color: var(--color-accent);">~{fmtCost(totalInventoryCost)}/mo</span>
				<span style="color: var(--color-muted);">across {filteredCostResources.length} resource{filteredCostResources.length !== 1 ? 's' : ''}</span>
				<span class="text-xs ml-auto" style="color: var(--color-muted);">Approximate on-demand prices, us-east-1. Actual costs may vary.</span>
			</div>
			<!-- Results table -->
			<div class="rounded-lg border overflow-hidden" style="border-color: var(--color-border);">
				<div class="overflow-x-auto">
					<table class="w-full text-sm">
						<thead>
							<tr style="background-color: var(--color-surface); border-bottom: 1px solid var(--color-border);">
								{#each ['Service','Name / ID','State','Size / Details','Est. Cost/mo'] as h}
									<th class="text-left px-4 py-3 font-semibold text-xs whitespace-nowrap" style="color: var(--color-muted);">{h}</th>
								{/each}
							</tr>
						</thead>
						<tbody>
							{#each filteredCostResources as r (r.resource_type + '/' + r.resource_id)}
								{@const meta = resourceTypeMeta(r.resource_type)}
								<tr
									style="border-bottom: 1px solid var(--color-border);"
									onmouseenter={(e) => (e.currentTarget as HTMLElement).style.backgroundColor = 'var(--color-surface)'}
									onmouseleave={(e) => (e.currentTarget as HTMLElement).style.backgroundColor = ''}
								>
									<td class="px-4 py-3">
										<span class="inline-block rounded px-2 py-0.5 text-xs font-medium"
											style="background-color: {meta.color}22; color: {meta.color};">{meta.label}</span>
									</td>
									<td class="px-4 py-3">
										<span class="font-medium">{r.name}</span>
										{#if r.name !== r.resource_id}
											<span class="block font-mono text-xs" style="color: var(--color-muted);">{r.resource_id}</span>
										{/if}
									</td>
									<td class="px-4 py-3">
										<span class="inline-flex items-center gap-1 text-xs rounded-full px-2 py-0.5"
											style="background-color: {r.state === 'running' || r.state === 'available' || r.state === 'active' ? '#22c55e22' : '#71717a22'}; color: {r.state === 'running' || r.state === 'available' || r.state === 'active' ? '#22c55e' : '#71717a'};">
											{r.state}
										</span>
									</td>
									<td class="px-4 py-3 text-xs" style="color: var(--color-muted);">{r.size_hint ?? '—'}</td>
									<td class="px-4 py-3 font-semibold whitespace-nowrap" style="color: var(--color-text);">
										{#if r.estimated_monthly_usd > 0}
											~{fmtCost(r.estimated_monthly_usd)}/mo
										{:else}
											<span style="color: var(--color-muted);">—</span>
										{/if}
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			</div>
		{:else if costResourcesLoaded && filteredCostResources.length === 0}
			<div class="rounded-lg border p-10 text-center" style="border-color: var(--color-border); border-style: dashed;">
				<p class="text-sm font-medium mb-1">No billable resources found</p>
				<p class="text-xs" style="color: var(--color-muted);">No cost-generating resources were detected in {region} with the current credentials.</p>
			</div>
		{:else if !costResourcesLoading}
			<div class="rounded-lg border p-10 text-center" style="border-color: var(--color-border); border-style: dashed;">
				<svg class="w-8 h-8 mx-auto mb-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" style="color: var(--color-muted);">
					<path d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0z"/>
				</svg>
				<p class="text-sm font-medium mb-1">Cost Inventory</p>
				<p class="text-xs mb-4" style="color: var(--color-muted);">
					Query EC2, EBS, RDS, NAT Gateways, Load Balancers, Elastic IPs, Snapshots, and ElastiCache<br/>
					to see every resource currently incurring charges in {region}.
				</p>
				<button onclick={loadCostInventory} class="inline-flex items-center gap-2 rounded px-4 py-2 text-sm font-medium" style="background-color: var(--color-accent); color: white;">
					Load Cost Inventory
				</button>
			</div>
		{/if}
	</div>

	{:else}
	<!-- Tag Resource Search -->
	<div class="mb-4">
		<div class="flex flex-wrap items-center gap-2 mb-3">
			<input
				type="text"
				placeholder="Tag key (e.g. Owner)"
				bind:value={tagKey}
				onkeydown={(e) => e.key === 'Enter' && runTagSearch()}
				class="rounded px-2.5 py-1.5 text-sm border w-44"
				style="background-color: var(--color-surface); border-color: var(--color-border); color: var(--color-text);"
			/>
			<input
				type="text"
				placeholder="Value (optional)"
				bind:value={tagValue}
				onkeydown={(e) => e.key === 'Enter' && runTagSearch()}
				class="rounded px-2.5 py-1.5 text-sm border w-44"
				style="background-color: var(--color-surface); border-color: var(--color-border); color: var(--color-text);"
			/>
			<button
				onclick={runTagSearch}
				disabled={tagLoading || !tagKey.trim()}
				class="flex items-center gap-1.5 rounded px-3 py-1.5 text-sm font-medium transition-colors disabled:opacity-50"
				style="background-color: var(--color-accent); color: white;"
			>
				{#if tagLoading}
					<svg class="w-3.5 h-3.5 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 11-6.219-8.56"/></svg>
					Searching…
				{:else}
					Search
				{/if}
			</button>
			{#if tagSearched}
				<button
					onclick={() => { tagKey = ''; tagValue = ''; tagResults = []; tagSearched = false; tagError = null; }}
					class="text-xs rounded px-2.5 py-1.5 border transition-colors"
					style="border-color: var(--color-border); color: var(--color-muted);"
				>Clear</button>
			{/if}
		</div>

		{#if tagError}
			<div class="mb-3 rounded border px-4 py-3 text-sm" style="background-color: var(--color-error-bg); border-color: var(--color-error-border); color: var(--color-error-text);">{tagError}</div>
		{/if}

		{#if tagSearched && !tagLoading}
			<p class="text-xs mb-2" style="color: var(--color-muted);">
				{tagResults.length} resource{tagResults.length !== 1 ? 's' : ''} found
				{#if tagKey}matching <code class="font-mono">{tagKey}{tagValue ? '=' + tagValue : ''}</code>{/if}
				in {region}
			</p>
		{/if}

		{#if tagResults.length > 0}
			<div class="rounded-lg border overflow-hidden" style="border-color: var(--color-border);">
				<div class="overflow-x-auto">
					<table class="w-full text-sm">
						<thead>
							<tr style="background-color: var(--color-surface); border-bottom: 1px solid var(--color-border);">
								{#each ['Service','Name / Resource','ARN','Tags'] as h}
									<th class="text-left px-4 py-3 font-semibold text-xs" style="color: var(--color-muted);">{h}</th>
								{/each}
							</tr>
						</thead>
						<tbody>
							{#each tagResults as r (r.arn)}
								{@const svcColors: Record<string, string> = { ec2: '#3b82f6', rds: '#f97316', lambda: '#a855f7', s3: '#eab308', ecs: '#06b6d4', eks: '#6366f1', elb: '#22c55e', elasticloadbalancing: '#22c55e', sns: '#ec4899', sqs: '#14b8a6', dynamodb: '#f43f5e' }}
								{@const svcColor = svcColors[r.service.toLowerCase()] ?? '#71717a'}
								<tr
									style="border-bottom: 1px solid var(--color-border);"
									onmouseenter={(e) => (e.currentTarget as HTMLElement).style.backgroundColor = 'var(--color-surface)'}
									onmouseleave={(e) => (e.currentTarget as HTMLElement).style.backgroundColor = ''}
								>
									<td class="px-4 py-3">
										<span class="inline-block rounded px-2 py-0.5 text-xs font-medium font-mono"
											style="background-color: {svcColor}22; color: {svcColor};">
											{r.service}
										</span>
									</td>
									<td class="px-4 py-3">
										{#if r.name}
											<span class="font-medium">{r.name}</span>
											<span class="block font-mono text-xs" style="color: var(--color-muted);">{r.resource}</span>
										{:else}
											<span class="font-mono text-xs">{r.resource}</span>
										{/if}
									</td>
									<td class="px-4 py-3 font-mono text-xs max-w-xs truncate" style="color: var(--color-muted);" title={r.arn}>{r.arn}</td>
									<td class="px-4 py-3">
										<div class="flex flex-wrap gap-1">
											{#each r.tags as tag}
												<span class="rounded px-1.5 py-0.5 text-xs font-mono" style="background-color: var(--color-border);">{tag.Key}: <span style="color: var(--color-muted);">{tag.Value}</span></span>
											{/each}
										</div>
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			</div>
		{:else if tagSearched && !tagLoading && !tagError}
			<div class="rounded-lg border p-10 text-center" style="border-color: var(--color-border); border-style: dashed;">
				<p class="text-sm font-medium mb-1">No resources found</p>
				<p class="text-xs" style="color: var(--color-muted);">No resources in {region} have the tag <code class="font-mono">{tagKey}{tagValue ? '=' + tagValue : ''}</code></p>
			</div>
		{:else if !tagSearched}
			<div class="rounded-lg border p-10 text-center" style="border-color: var(--color-border); border-style: dashed;">
				<svg class="w-8 h-8 mx-auto mb-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" style="color: var(--color-muted);">
					<path d="M7 7h.01M7 3h5c.512 0 1.024.195 1.414.586l7 7a2 2 0 0 1 0 2.828l-7 7a2 2 0 0 1-2.828 0l-7-7A2 2 0 0 1 3 12V7a4 4 0 0 1 4-4z"/>
				</svg>
				<p class="text-sm font-medium mb-1">Search resources by tag</p>
				<p class="text-xs" style="color: var(--color-muted);">Enter a tag key (and optional value) to find all AWS resources in {region} with that tag.</p>
			</div>
		{/if}
	</div>
	{/if}
{/if}

<!-- Bottom spacer so content isn't hidden behind the log tray -->
<div class="h-10"></div>

<!-- Log tray (fixed bottom) -->
<div class="fixed bottom-0 left-0 right-0 z-50 flex flex-col" style="border-top: 1px solid var(--color-border);">
	{#if logsOpen}
		<div bind:this={logScrollEl} class="overflow-y-auto font-mono text-xs"
			style="height: 200px; background-color: var(--color-bg); transition: height 0.2s ease;">
			{#if logEntries.length === 0}
				<p class="px-4 py-3" style="color: var(--color-muted);">No log entries yet.</p>
			{:else}
				{#each logEntries as entry (entry.id)}
					<div class="flex gap-2 px-3 py-0.5 border-b items-baseline"
						style="border-color: var(--color-border); background-color: {entry.level === 'error' ? 'var(--color-error-bg)' : entry.level === 'warn' ? 'var(--color-warn-bg)' : 'transparent'};">
						<span class="shrink-0 tabular-nums" style="color: var(--color-muted);">{fmtTime(entry.time)}</span>
						<span class="shrink-0 w-10 font-bold" style="color: {entry.level === 'error' ? '#f87171' : entry.level === 'warn' ? '#fbbf24' : '#6ee7b7'};">{entry.level.toUpperCase()}</span>
						<span class="shrink-0 w-20" style="color: #6366f1;">[{entry.source}]</span>
						<span class="flex-1 break-all" style="color: {entry.level === 'error' ? 'var(--color-error-text)' : entry.level === 'warn' ? 'var(--color-warn-text)' : 'var(--color-text)'};">{entry.message}</span>
						{#if entry.duration !== undefined}
							<span class="shrink-0 tabular-nums" style="color: var(--color-muted);">{fmtDuration(entry.duration)}</span>
						{/if}
					</div>
				{/each}
			{/if}
		</div>
	{/if}
	<div class="flex items-center gap-2 px-3 py-1.5 cursor-pointer select-none"
		style="background-color: var(--color-surface);"
		onclick={() => logsOpen = !logsOpen} role="button" tabindex="0"
		onkeydown={(e) => e.key === 'Enter' && (logsOpen = !logsOpen)}>
		<svg class="w-3 h-3 transition-transform" style="color: var(--color-muted); transform: rotate({logsOpen ? '180deg' : '0deg'});" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="18 15 12 9 6 15"/></svg>
		<span class="text-xs font-semibold" style="color: var(--color-muted);">LOGS</span>
		{#if errorCount > 0}
			<span class="rounded px-1.5 py-0.5 text-xs font-bold" style="background-color: var(--color-error-bg); border: 1px solid var(--color-error-border); color: var(--color-error-text);">{errorCount} error{errorCount !== 1 ? 's' : ''}</span>
		{/if}
		{#if !logsOpen && latestEntry}
			<span class="font-mono text-xs truncate flex-1" style="color: {latestEntry.level === 'error' ? '#f87171' : latestEntry.level === 'warn' ? '#fbbf24' : '#4b5563'};">{fmtTime(latestEntry.time)} [{latestEntry.source}] {latestEntry.message}</span>
		{/if}
		<div class="ml-auto flex items-center gap-2">
			{#if logEntries.length > 0}
				<button onclick={(e) => { e.stopPropagation(); logEntries = []; }} class="text-xs rounded px-2 py-0.5 border transition-colors" style="border-color: var(--color-border); color: var(--color-muted);">Clear</button>
			{/if}
			<span class="text-xs tabular-nums" style="color: var(--color-muted);">{logEntries.length}</span>
		</div>
	</div>
</div>
