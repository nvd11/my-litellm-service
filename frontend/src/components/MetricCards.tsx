import React from "react";
import { MessageSquare, Coins, CircleDollarSign, Zap, CheckCircle2 } from "lucide-react";
import { SummaryMetrics } from "../types";

interface MetricCardsProps {
  metrics: SummaryMetrics | null;
  loading: boolean;
}

export const MetricCards: React.FC<MetricCardsProps> = ({ metrics, loading }) => {
  const formatTokens = (tokens: number) => {
    if (tokens >= 1_000_000) {
      return (tokens / 1_000_000).toFixed(2) + " M";
    }
    if (tokens >= 1_000) {
      return (tokens / 1_000).toFixed(1) + " k";
    }
    return tokens.toLocaleString();
  };

  return (
    <div className="space-y-4">
      {/* 4 Main Summary Metric Cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        {/* Card 1: Today Invocations */}
        <div className="bg-slate-900 border border-slate-800 rounded-xl p-4.5 shadow-sm hover:border-slate-700 transition-all">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">今日调用总量</span>
            <div className="w-8 h-8 rounded-lg bg-blue-500/10 text-blue-400 flex items-center justify-center">
              <MessageSquare className="w-4 h-4" />
            </div>
          </div>
          <div className="mt-3 flex items-baseline gap-2">
            <span className="text-2xl font-bold text-slate-100">
              {loading && !metrics ? "..." : metrics?.today_requests.toLocaleString() ?? 0}
            </span>
            <span className="text-xs text-slate-500 font-medium">次请求</span>
          </div>
          <div className="mt-2 flex items-center gap-1.5 text-xs text-emerald-400">
            <CheckCircle2 className="w-3.5 h-3.5" />
            <span>成功率 {metrics?.success_rate ?? 100}%</span>
          </div>
        </div>

        {/* Card 2: Tokens Consumed */}
        <div className="bg-slate-900 border border-slate-800 rounded-xl p-4.5 shadow-sm hover:border-slate-700 transition-all">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">总消耗 Tokens</span>
            <div className="w-8 h-8 rounded-lg bg-purple-500/10 text-purple-400 flex items-center justify-center">
              <Coins className="w-4 h-4" />
            </div>
          </div>
          <div className="mt-3 flex items-baseline gap-2">
            <span className="text-2xl font-bold text-purple-300">
              {loading && !metrics ? "..." : formatTokens(metrics?.today_tokens ?? 0)}
            </span>
            <span className="text-xs text-slate-500 font-medium">Tokens</span>
          </div>
          <p className="mt-2 text-xs text-slate-400">含输入 Context 与模型输出</p>
        </div>

        {/* Card 3: CNY Cost */}
        <div className="bg-slate-900 border border-slate-800 rounded-xl p-4.5 shadow-sm hover:border-slate-700 transition-all">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">折合人民币扣费</span>
            <div className="w-8 h-8 rounded-lg bg-emerald-500/10 text-emerald-400 flex items-center justify-center">
              <CircleDollarSign className="w-4 h-4" />
            </div>
          </div>
          <div className="mt-3 flex items-baseline gap-2">
            <span className="text-2xl font-bold text-emerald-400">
              ¥ {loading && !metrics ? "..." : (metrics?.today_cost_cny ?? 0).toFixed(4)}
            </span>
            <span className="text-xs text-slate-500">(${(metrics?.today_cost_usd ?? 0).toFixed(4)})</span>
          </div>
          <p className="mt-2 text-xs text-slate-400">按当日中行实时汇率动态折算</p>
        </div>

        {/* Card 4: Average Latency */}
        <div className="bg-slate-900 border border-slate-800 rounded-xl p-4.5 shadow-sm hover:border-slate-700 transition-all">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">平均响应延迟</span>
            <div className="w-8 h-8 rounded-lg bg-amber-500/10 text-amber-400 flex items-center justify-center">
              <Zap className="w-4 h-4" />
            </div>
          </div>
          <div className="mt-3 flex items-baseline gap-2">
            <span className="text-2xl font-bold text-amber-300">
              {loading && !metrics ? "..." : (metrics?.avg_latency_ms ?? 0).toLocaleString()}
            </span>
            <span className="text-xs text-slate-500 font-medium">ms</span>
          </div>
          <p className="mt-2 text-xs text-slate-400">端到端网络与推理时延</p>
        </div>
      </div>

      {/* Active Keys & Models Distribution Bar */}
      {metrics?.active_keys && metrics.active_keys.length > 0 && (
        <div className="bg-slate-900/60 border border-slate-800/80 rounded-xl p-3.5 flex flex-wrap items-center justify-between gap-3 text-xs">
          <div className="flex items-center gap-2">
            <span className="text-slate-400 font-semibold">活跃 Key 消耗分布:</span>
            <div className="flex flex-wrap items-center gap-2">
              {metrics.active_keys.map((k) => (
                <div
                  key={k.alias}
                  className="bg-slate-800 border border-slate-700/80 rounded-lg px-2.5 py-1 flex items-center gap-2"
                >
                  <span className="font-semibold text-blue-400">{k.alias}</span>
                  <span className="text-slate-400">{k.count} 次</span>
                  <span className="text-emerald-400 font-mono">¥{k.cost_cny.toFixed(4)}</span>
                </div>
              ))}
            </div>
          </div>

          {metrics.models_breakdown && metrics.models_breakdown.length > 0 && (
            <div className="flex items-center gap-2">
              <span className="text-slate-400 font-semibold">模型分布:</span>
              <div className="flex items-center gap-2">
                {metrics.models_breakdown.map((m) => (
                  <span
                    key={m.model}
                    className="bg-purple-900/20 text-purple-300 border border-purple-700/30 px-2 py-0.5 rounded font-mono text-[11px]"
                  >
                    {m.model} ({m.count})
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
};
