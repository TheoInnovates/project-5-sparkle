// Regional price multipliers relative to us-east-1.
// Source: AWS public pricing pages. Shown as estimates only.
export const REGION_PRICE_MULTIPLIER: Record<string, number> = {
	'us-east-1':      1.000,
	'us-east-2':      1.000,
	'us-west-1':      1.057,
	'us-west-2':      1.000,
	'ca-central-1':   1.070,
	'ca-west-1':      1.090,
	'eu-west-1':      1.090,
	'eu-west-2':      1.117,
	'eu-west-3':      1.126,
	'eu-central-1':   1.117,
	'eu-central-2':   1.150,
	'eu-north-1':     1.070,
	'eu-south-1':     1.110,
	'eu-south-2':     1.130,
	'ap-east-1':      1.160,
	'ap-southeast-1': 1.098,
	'ap-southeast-2': 1.114,
	'ap-southeast-3': 1.120,
	'ap-southeast-4': 1.120,
	'ap-northeast-1': 1.140,
	'ap-northeast-2': 1.070,
	'ap-northeast-3': 1.140,
	'ap-south-1':     1.070,
	'ap-south-2':     1.110,
	'sa-east-1':      1.150,
	'me-south-1':     1.175,
	'me-central-1':   1.150,
	'af-south-1':     1.160,
	'il-central-1':   1.160,
	'us-gov-east-1':  1.150,
	'us-gov-west-1':  1.150,
};

export function regionMultiplier(region?: string): number {
	if (!region) return 1;
	return REGION_PRICE_MULTIPLIER[region] ?? 1;
}

// Approximate on-demand Linux $/hr — us-east-1 base prices.
// Use estimateInstanceCostPerMonth(type, region) for region-adjusted values.
export const EC2_PRICE: Record<string, number> = {
	// T3 (burstable)
	't3.nano': 0.0052, 't3.micro': 0.0104, 't3.small': 0.0208,
	't3.medium': 0.0416, 't3.large': 0.0832, 't3.xlarge': 0.1664, 't3.2xlarge': 0.3328,
	// T3a (AMD)
	't3a.nano': 0.0047, 't3a.micro': 0.0094, 't3a.small': 0.0188,
	't3a.medium': 0.0376, 't3a.large': 0.0752, 't3a.xlarge': 0.1504, 't3a.2xlarge': 0.3008,
	// T4g (Graviton2)
	't4g.nano': 0.0042, 't4g.micro': 0.0084, 't4g.small': 0.0168,
	't4g.medium': 0.0336, 't4g.large': 0.0672, 't4g.xlarge': 0.1344, 't4g.2xlarge': 0.2688,
	// M5
	'm5.large': 0.096, 'm5.xlarge': 0.192, 'm5.2xlarge': 0.384,
	'm5.4xlarge': 0.768, 'm5.8xlarge': 1.536, 'm5.12xlarge': 2.304, 'm5.16xlarge': 3.072,
	// M6i
	'm6i.large': 0.096, 'm6i.xlarge': 0.192, 'm6i.2xlarge': 0.384,
	'm6i.4xlarge': 0.768, 'm6i.8xlarge': 1.536,
	// M6g (Graviton2)
	'm6g.large': 0.077, 'm6g.xlarge': 0.154, 'm6g.2xlarge': 0.308, 'm6g.4xlarge': 0.616,
	// C5
	'c5.large': 0.085, 'c5.xlarge': 0.17, 'c5.2xlarge': 0.34,
	'c5.4xlarge': 0.68, 'c5.9xlarge': 1.53, 'c5.18xlarge': 3.06,
	// C6i
	'c6i.large': 0.085, 'c6i.xlarge': 0.17, 'c6i.2xlarge': 0.34, 'c6i.4xlarge': 0.68,
	// C6g (Graviton2)
	'c6g.large': 0.068, 'c6g.xlarge': 0.136, 'c6g.2xlarge': 0.272, 'c6g.4xlarge': 0.544,
	// R5
	'r5.large': 0.126, 'r5.xlarge': 0.252, 'r5.2xlarge': 0.504,
	'r5.4xlarge': 1.008, 'r5.8xlarge': 2.016,
	// R6i
	'r6i.large': 0.126, 'r6i.xlarge': 0.252, 'r6i.2xlarge': 0.504, 'r6i.4xlarge': 1.008,
	// G4dn (GPU)
	'g4dn.xlarge': 0.526, 'g4dn.2xlarge': 0.752, 'g4dn.4xlarge': 1.204, 'g4dn.8xlarge': 2.264,
	// P3
	'p3.2xlarge': 3.06, 'p3.8xlarge': 12.24, 'p3.16xlarge': 24.48,
};

// EBS $/GB-month
export const EBS_PRICE: Record<string, number> = {
	'gp3': 0.08, 'gp2': 0.10,
	'io1': 0.125, 'io2': 0.125,
	'st1': 0.045, 'sc1': 0.018,
	'standard': 0.05,
};

export function estimateInstanceCostPerHour(type: string, region?: string): number | null {
	const base = EC2_PRICE[type];
	return base != null ? base * regionMultiplier(region) : null;
}

export function estimateInstanceCostPerMonth(type: string, region?: string): number | null {
	const rate = estimateInstanceCostPerHour(type, region);
	return rate != null ? rate * 730 : null;
}

export function estimateEBSCostPerMonth(volumeType: string, sizeGb: number, region?: string): number {
	const base = EBS_PRICE[volumeType] ?? 0.08;
	return base * sizeGb * regionMultiplier(region);
}

export function fmtCost(dollars: number): string {
	if (dollars < 1) return `$${dollars.toFixed(3)}`;
	if (dollars < 100) return `$${dollars.toFixed(2)}`;
	return `$${Math.round(dollars).toLocaleString()}`;
}

// Service display metadata for cost inventory
export const RESOURCE_TYPE_META: Record<string, { label: string; color: string }> = {
	ec2_instance:   { label: 'EC2',         color: '#3b82f6' },
	ebs_volume:     { label: 'EBS',         color: '#6366f1' },
	ebs_snapshot:   { label: 'Snapshot',    color: '#8b5cf6' },
	rds_instance:   { label: 'RDS',         color: '#f97316' },
	aurora_cluster: { label: 'Aurora',      color: '#fb923c' },
	nat_gateway:    { label: 'NAT GW',      color: '#06b6d4' },
	load_balancer:  { label: 'ELB',         color: '#22c55e' },
	elastic_ip:     { label: 'Elastic IP',  color: '#eab308' },
	elasticache:    { label: 'ElastiCache', color: '#ec4899' },
};

export function resourceTypeMeta(type: string): { label: string; color: string } {
	return RESOURCE_TYPE_META[type] ?? { label: type, color: '#71717a' };
}
