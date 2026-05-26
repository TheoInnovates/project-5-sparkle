<script lang="ts">
	import '../app.css';
	import { onMount } from 'svelte';
	import CredentialsPanel from '$lib/CredentialsPanel.svelte';
	import { getConfig } from '$lib/api';

	const { children } = $props();

	let envConfigured = $state(false);

	onMount(async () => {
		try {
			const cfg = await getConfig();
			envConfigured = cfg.env_creds_configured;
		} catch {}
	});

	function onCredentialsSaved() {
		window.dispatchEvent(new CustomEvent('sparkle:credentials-saved'));
	}
</script>

<div class="min-h-screen" style="background-color: var(--color-bg); color: var(--color-text);">
	<header class="border-b px-6 py-3 flex items-center gap-3" style="border-color: var(--color-border); background-color: var(--color-surface);">
		<svg class="w-5 h-5" style="color: var(--color-accent);" viewBox="0 0 24 24" fill="currentColor">
			<path d="M12 2L9.19 8.63L2 9.24l5.46 4.73L5.82 21 12 17.27 18.18 21l-1.64-7.03L22 9.24l-7.19-.61L12 2z"/>
		</svg>
		<span class="font-semibold tracking-wide">Sparkle</span>
		<span class="text-sm" style="color: var(--color-muted);">EC2 Instance Viewer</span>
		<div class="ml-auto">
			<CredentialsPanel onSave={onCredentialsSaved} {envConfigured} />
		</div>
	</header>
	<main class="p-6">
		{@render children()}
	</main>
</div>
