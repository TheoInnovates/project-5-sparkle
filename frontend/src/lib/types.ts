export interface InstanceRecord {
	instance_id: string;
	name: string;
	state: string;
	instance_type: string;
	availability_zone: string;
	launch_time: string;
	first_started: string | null;
	username: string | null;
}

export type CredSource = 'local' | 'env' | 'manual';

export interface CredConfig {
	source: CredSource;
	accessKeyId?: string;
	secretAccessKey?: string;
	sessionToken?: string;
}

export interface Config {
	default_region: string;
	env_creds_configured: boolean;
}
