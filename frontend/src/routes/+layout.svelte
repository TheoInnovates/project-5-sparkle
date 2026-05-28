<script lang="ts">
	import '../app.css';
	import { onMount } from 'svelte';
	import CredentialsPanel from '$lib/CredentialsPanel.svelte';
	import { getConfig } from '$lib/api';

	const { children } = $props();

	let envConfigured = $state(false);
	let theme = $state<'dark' | 'light'>('dark');

	onMount(async () => {
		try {
			const cfg = await getConfig();
			envConfigured = cfg.env_creds_configured;
		} catch {}

		const saved = localStorage.getItem('vantage-theme') as 'dark' | 'light' | null;
		theme = saved ?? (window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
		document.documentElement.setAttribute('data-theme', theme);
	});

	function toggleTheme() {
		theme = theme === 'dark' ? 'light' : 'dark';
		localStorage.setItem('vantage-theme', theme);
		document.documentElement.setAttribute('data-theme', theme);
	}

	function onCredentialsSaved() {
		window.dispatchEvent(new CustomEvent('vantage:credentials-saved'));
	}
</script>

<div class="min-h-screen" style="background-color: var(--color-bg); color: var(--color-text);">
	<header class="px-5 py-3.5 flex items-center gap-3" style="background-color: var(--color-surface); box-shadow: 0 1px 0 var(--color-border);">
		<!-- Logo mark: diamond + dot -->
		<svg class="w-5 h-5 shrink-0" style="color: var(--color-accent);" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linejoin="round">
			<polygon points="12,2 22,12 12,22 2,12"/>
			<circle cx="12" cy="12" r="2.5" fill="currentColor" stroke="none"/>
		</svg>

		<span class="text-sm font-semibold tracking-tight">Vantage</span>
		<span class="hidden sm:block text-xs" style="color: var(--color-muted);">AWS Resource Viewer</span>

		<div class="ml-auto flex items-center gap-2">
			<!-- Theme toggle -->
			<button
				onclick={toggleTheme}
				title={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
				class="rounded-lg p-1.5 transition-colors"
				style="color: var(--color-muted); background: transparent;"
				onmouseenter={(e) => (e.currentTarget as HTMLElement).style.backgroundColor = 'var(--color-surface-raised)'}
				onmouseleave={(e) => (e.currentTarget as HTMLElement).style.backgroundColor = 'transparent'}
			>
				{#if theme === 'dark'}
					<!-- Sun icon -->
					<svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
						<circle cx="12" cy="12" r="4"/>
						<path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/>
					</svg>
				{:else}
					<!-- Moon icon -->
					<svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
						<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
					</svg>
				{/if}
			</button>

			<CredentialsPanel onSave={onCredentialsSaved} {envConfigured} />
		</div>
	</header>
	<main class="p-5">
		{@render children()}
	</main>
</div>
