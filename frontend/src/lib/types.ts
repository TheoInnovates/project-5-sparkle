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
	private_ip: string | null;
	public_ip: string | null;
	vpc_id: string | null;
	subnet_id: string | null;
	security_groups: { id: string; name: string }[] | null;
	image_id: string | null;
	key_name: string | null;
	iam_profile: string | null;
	architecture: string | null;
	tags: { Key: string; Value: string }[] | null;
	region?: string;
}

export interface TagResource {
	arn: string;
	service: string;
	resource: string;
	name: string | null;
	tags: { Key: string; Value: string }[];
}

export interface EBSVolume {
	volume_id: string;
	state: string;
	size_gb: number;
	volume_type: string;
	iops: number | null;
	throughput: number | null;
	create_time: string;
	name: string | null;
	attachments: { instance_id: string; device: string }[];
	tags: { Key: string; Value: string }[];
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

export interface CostResource {
	resource_type: string;
	resource_id: string;
	name: string;
	arn: string | null;
	state: string;
	region: string;
	created_at: string | null;
	size_hint: string | null;
	estimated_monthly_usd: number;
	tags: { Key: string; Value: string }[];
}
