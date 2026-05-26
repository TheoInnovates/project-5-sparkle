<script lang="ts">
	import { clearCredConfig, loadCredConfig, saveCredConfig } from './api';
	import type { CredConfig, CredSource } from './types';

	let { onSave, envConfigured = false }: { onSave?: () => void; envConfigured?: boolean } = $props();

	const stored = loadCredConfig();
	let open = $state(stored.source === 'local' && !envConfigured ? false : false);
	let source = $state<CredSource>(stored.source);
	let accessKeyId = $state(stored.accessKeyId ?? '');
	let secretAccessKey = $state(stored.secretAccessKey ?? '');
	let sessionToken = $state(stored.sessionToken ?? '');
	let showSecret = $state(false);
	let showToken = $state(false);

	const LABELS: Record<CredSource, string> = {
		local: 'Local / Profile',
		env: '.env file',
		manual: 'Manual entry',
	};

	const saved = $derived(
		source === 'local' ||
		(source === 'env' && envConfigured) ||
		(source === 'manual' && !!stored.accessKeyId)
	);

	function handleSave() {
		const config: CredConfig = { source };
		if (source === 'manual') {
			if (!accessKeyId.trim() || !secretAccessKey.trim()) return;
			config.accessKeyId = accessKeyId.trim();
			config.secretAccessKey = secretAccessKey.trim();
			config.sessionToken = sessionToken.trim() || undefined;
		}
		saveCredConfig(config);
		open = false;
		onSave?.();
	}

	function handleClear() {
		clearCredConfig();
		source = 'local';
		accessKeyId = '';
		secretAccessKey = '';
		sessionToken = '';
		open = false;
		onSave?.();
	}

	const buttonLabel = $derived(LABELS[source] ?? 'Set credentials');
	const buttonOk = $derived(saved);
</script>

<div class="relative">
	<!-- Trigger button -->
	<button
		onclick={() => (open = !open)}
		class="flex items-center gap-1.5 rounded px-2.5 py-1.5 text-xs font-medium border transition-colors"
		style="border-color: var(--color-border); background-color: {buttonOk ? '#14532d22' : '#27272a'}; color: {buttonOk ? '#86efac' : '#a1a1aa'};"
		title="Credential source: {buttonLabel} — click to change"
	>
		<svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
			<rect x="3" y="11" width="18" height="11" rx="2" ry="2"/>
			<path d="M7 11V7a5 5 0 0 1 10 0v4"/>
		</svg>
		{buttonLabel}
	</button>

	<!-- Dropdown panel -->
	{#if open}
		<div
			class="absolute right-0 top-full mt-1 z-50 rounded-lg border p-4 w-80 shadow-xl"
			style="background-color: var(--color-surface); border-color: var(--color-border);"
		>
			<p class="text-xs font-semibold mb-3" style="color: var(--color-muted);">CREDENTIAL SOURCE</p>

			<!-- Source radio group -->
			<div class="flex flex-col gap-1 mb-4">
				{#each (['local', 'env', 'manual'] as CredSource[]) as s}
					{@const isEnvDisabled = s === 'env' && !envConfigured}
					<label
						class="flex items-start gap-2.5 rounded px-2.5 py-2 cursor-pointer transition-colors"
						style="background-color: {source === s ? 'var(--color-accent)22' : 'transparent'}; {isEnvDisabled ? 'opacity: 0.45; cursor: not-allowed;' : ''}"
					>
						<input
							type="radio"
							name="cred-source"
							value={s}
							bind:group={source}
							disabled={isEnvDisabled}
							class="mt-0.5 accent-[var(--color-accent)]"
						/>
						<div>
							<div class="text-sm font-medium">{LABELS[s]}</div>
							<div class="text-xs mt-0.5" style="color: var(--color-muted);">
								{#if s === 'local'}
									AWS CLI profile, ~/.aws/credentials, or IAM role
								{:else if s === 'env'}
									{#if envConfigured}
										AWS_ACCESS_KEY_ID from the server .env file
									{:else}
										Not configured — set AWS_ACCESS_KEY_ID in .env
									{/if}
								{:else}
									Enter keys directly in the form below
								{/if}
							</div>
						</div>
					</label>
				{/each}
			</div>

			<!-- Manual fields — only shown when manual is selected -->
			{#if source === 'manual'}
				<div class="border-t pt-3 mb-4" style="border-color: var(--color-border);">
					<label class="block mb-3">
						<span class="text-xs mb-1 block" style="color: var(--color-muted);">Access Key ID <span style="color: #f87171;">*</span></span>
						<input
							type="text"
							bind:value={accessKeyId}
							placeholder="AKIAIOSFODNN7EXAMPLE"
							class="w-full rounded px-2.5 py-1.5 text-sm border font-mono"
							style="background-color: var(--color-bg); border-color: var(--color-border); color: var(--color-text);"
						/>
					</label>

					<label class="block mb-3">
						<span class="text-xs mb-1 block" style="color: var(--color-muted);">Secret Access Key <span style="color: #f87171;">*</span></span>
						<div class="relative">
							<input
								type={showSecret ? 'text' : 'password'}
								bind:value={secretAccessKey}
								placeholder="••••••••••••••••••••••••"
								class="w-full rounded px-2.5 py-1.5 pr-8 text-sm border font-mono"
								style="background-color: var(--color-bg); border-color: var(--color-border); color: var(--color-text);"
							/>
							<button type="button" onclick={() => (showSecret = !showSecret)} class="absolute right-2 top-1/2 -translate-y-1/2" style="color: var(--color-muted);" tabindex="-1">
								{#if showSecret}
									<svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/><path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/><line x1="1" y1="1" x2="23" y2="23"/></svg>
								{:else}
									<svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
								{/if}
							</button>
						</div>
					</label>

					<label class="block">
						<span class="text-xs mb-1 block" style="color: var(--color-muted);">Session Token <span class="opacity-50">(optional)</span></span>
						<div class="relative">
							<input
								type={showToken ? 'text' : 'password'}
								bind:value={sessionToken}
								placeholder="Optional — for temporary credentials"
								class="w-full rounded px-2.5 py-1.5 pr-8 text-sm border font-mono"
								style="background-color: var(--color-bg); border-color: var(--color-border); color: var(--color-text);"
							/>
							<button type="button" onclick={() => (showToken = !showToken)} class="absolute right-2 top-1/2 -translate-y-1/2" style="color: var(--color-muted);" tabindex="-1">
								{#if showToken}
									<svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/><path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/><line x1="1" y1="1" x2="23" y2="23"/></svg>
								{:else}
									<svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
								{/if}
							</button>
						</div>
					</label>
				</div>
			{/if}

			<div class="flex gap-2">
				<button
					onclick={handleSave}
					disabled={source === 'manual' && (!accessKeyId.trim() || !secretAccessKey.trim())}
					class="flex-1 rounded px-3 py-1.5 text-sm font-medium transition-colors disabled:opacity-40"
					style="background-color: var(--color-accent); color: white;"
				>
					Apply
				</button>
				<button
					onclick={handleClear}
					class="rounded px-3 py-1.5 text-sm border transition-colors"
					style="border-color: var(--color-border); color: var(--color-muted);"
					title="Reset to Local / Profile"
				>
					Reset
				</button>
				<button
					onclick={() => (open = false)}
					class="rounded px-3 py-1.5 text-sm border transition-colors"
					style="border-color: var(--color-border); color: var(--color-muted);"
				>
					Cancel
				</button>
			</div>

			{#if source === 'manual'}
				<p class="mt-3 text-xs" style="color: var(--color-muted);">
					Keys stored in <code>sessionStorage</code> — cleared when you close this tab.
				</p>
			{/if}
		</div>

		<!-- Click-outside overlay -->
		<div class="fixed inset-0 z-40" role="presentation" onclick={() => (open = false)}></div>
	{/if}
</div>
