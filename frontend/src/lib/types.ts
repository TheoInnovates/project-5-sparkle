export interface InstanceEvent {
	event_time: string;
	event_name: string;
	instance_id: string;
	username: string | null;
	source_ip: string | null;
}

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
	region?: string;
	accessKeyId?: string;
	secretAccessKey?: string;
	sessionToken?: string;
}

export interface Config {
	default_region: string;
	env_creds_configured: boolean;
}
