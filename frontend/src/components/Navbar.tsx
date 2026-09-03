import React from "react";
import { Activity, RefreshCw, Calendar, Sparkles } from "lucide-react";

interface NavbarProps {
  selectedDate: string;
  onDateChange: (date: string) => void;
  autoRefresh: number;
  onAutoRefreshChange: (seconds: number) => void;
  onManualRefresh: () => void;
  loading: boolean;
}

export const Navbar: React.FC<NavbarProps> = ({
  selectedDate,
  onDateChange,
  autoRefresh,
  onAutoRefreshChange,
  onManualRefresh,
  loading,
}) => {
  return (
    <header className="sticky top-0 z-30 bg-slate-900/80 backdrop-blur-md border-b border-slate-800 px-6 py-3.5">
      <div className="max-w-7xl mx-auto flex flex-col md:flex-row items-center justify-between gap-4">
        {/* Logo & Brand */}
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-blue-600 to-indigo-600 flex items-center justify-center shadow-lg shadow-blue-500/20">
            <Sparkles className="w-5 h-5 text-white" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-lg font-bold bg-gradient-to-r from-slate-100 to-slate-300 bg-clip-text text-transparent">
                LiteLLM Observatory
              </h1>
              <span className="px-2 py-0.5 text-xs font-semibold bg-blue-500/10 text-blue-400 border border-blue-500/20 rounded-full">
                Live Audit
              </span>
            </div>
            <p className="text-xs text-slate-400">大模型企业级实时调用审计与可观测看板</p>
          </div>
        </div>

        {/* Controls */}
        <div className="flex items-center flex-wrap gap-3">
          {/* Date Selector */}
          <div className="flex items-center gap-2 bg-slate-850 border border-slate-750 rounded-lg px-3 py-1.5 text-sm">
            <Calendar className="w-4 h-4 text-slate-400" />
            <input
              type="date"
              value={selectedDate}
              onChange={(e) => onDateChange(e.target.value)}
              className="bg-transparent text-slate-200 text-sm focus:outline-none cursor-pointer"
            />
          </div>

          {/* Auto Refresh Switch */}
          <div className="flex items-center gap-1.5 bg-slate-850 border border-slate-750 rounded-lg p-1 text-xs">
            <span className="text-slate-400 px-2 flex items-center gap-1">
              <Activity className="w-3.5 h-3.5 text-emerald-400" /> 轮询:
            </span>
            {[0, 5, 15, 30].map((sec) => (
              <button
                key={sec}
                onClick={() => onAutoRefreshChange(sec)}
                className={`px-2 py-1 rounded font-medium transition-all ${
                  autoRefresh === sec
                    ? "bg-blue-600 text-white shadow-sm"
                    : "text-slate-400 hover:text-slate-200 hover:bg-slate-800"
                }`}
              >
                {sec === 0 ? "关" : `${sec}s`}
              </button>
            ))}
          </div>

          {/* Manual Refresh Button */}
          <button
            onClick={onManualRefresh}
            disabled={loading}
            className="flex items-center gap-1.5 bg-slate-800 hover:bg-slate-700 text-slate-200 px-3 py-1.5 rounded-lg border border-slate-700 text-sm font-medium transition-all disabled:opacity-50 shadow-sm"
          >
            <RefreshCw className={`w-4 h-4 ${loading ? "animate-spin text-blue-400" : ""}`} />
            刷新
          </button>
        </div>
      </div>
    </header>
  );
};
