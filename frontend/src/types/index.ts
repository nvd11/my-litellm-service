export interface LogItem {
  id: string;
  request_id: string;
  api_key_alias: string;
  model_requested: string;
  model_used: string;
  provider: string;
  provider_key_alias: string;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cost_usd: number;
  cost_cny: number;
  fx_rate: number;
  latency_ms: number;
  status_code: number;
  error_msg?: string | null;
  created_at: string;
  prompt_url: string;
  response_url: string;
}

export interface PaginatedLogsResponse {
  items: LogItem[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

export interface ActiveKeyMetric {
  alias: string;
  count: number;
  tokens: number;
  cost_cny: number;
}

export interface ModelBreakdownMetric {
  model: string;
  count: number;
  tokens: number;
  cost_cny: number;
}

export interface SummaryMetrics {
  date: string;
  today_requests: number;
  today_tokens: number;
  today_cost_cny: number;
  today_cost_usd: number;
  avg_latency_ms: number;
  success_rate: number;
  active_keys: ActiveKeyMetric[];
  models_breakdown: ModelBreakdownMetric[];
}

export interface MessageItem {
  role: string;
  content: string | null;
}

export interface PromptPayload {
  model?: string;
  system_prompt?: string | null;
  user_prompt?: string | null;
  messages?: MessageItem[];
  parameters?: Record<string, any>;
  tools?: any;
}

export interface ResponsePayload {
  model?: string;
  reply?: string | null;
  reasoning_content?: string | null;
  tool_calls?: any;
  finish_reason?: string | null;
  usage?: {
    prompt_tokens?: number;
    completion_tokens?: number;
    total_tokens?: number;
  };
}

export interface PayloadData {
  request_id: string;
  date: string;
  prompt: PromptPayload;
  response: ResponsePayload;
  prompt_url: string;
  response_url: string;
}
