import React from "react";
import {
  Search,
  ChevronLeft,
  ChevronRight,
  CheckCircle,
  AlertTriangle,
  XCircle,
  FileText,
} from "lucide-react";
import { LogItem, PaginatedLogsResponse } from "../types";

interface LogsTableProps {
  logsData: PaginatedLogsResponse | null;
  loading: boolean;
  page: number;
  onPageChange: (page: number) => void;
  searchKeyword: string;
  onSearchChange: (val: string) => void;
  selectedKeyAlias: string;
  onKeyAliasChange: (val: string) => void;
  selectedModel: string;
  onModelChange: (val: string) => void;
  selectedStatusCode: string;
  onStatusCodeChange: (val: string) => void;
  onSelectLog: (log: LogItem) => void;
  selectedLogId: string | null;
}

export const LogsTable: React.FC<LogsTableProps> = ({
  logsData,
  loading,
  page,
  onPageChange,
  searchKeyword,
  onSearchChange,
  selectedKeyAlias,
  onKeyAliasChange,
  selectedModel,
  onModelChange,
  selectedStatusCode,
  onStatusCodeChange,
  onSelectLog,
  selectedLogId,
}) => {
  const getStatusBadge = (statusCode: number) => {
    if (statusCode === 200) {
      return (
        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
          <CheckCircle className="w-3 h-3" /> 200 OK
        </span>
      );
    }
    if (statusCode === 429) {
      return (
        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-amber-500/10 text-amber-400 border border-amber-500/20">
          <AlertTriangle className="w-3 h-3" /> 429 Limit
        </span>
      );
    }
    return (
      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-rose-500/10 text-rose-400 border border-rose-500/20">
        <XCircle className="w-3 h-3" /> {statusCode} Error
      </span>
    );
  };

  const formatTime = (timeStr: string) => {
    try {
      const d = new Date(timeStr);
      return d.toLocaleTimeString("zh-CN", {
        hour12: false,
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      });
    } catch {
      return timeStr;
    }
  };

  return (
    <div className="bg-white border border-slate-200 rounded-xl overflow-hidden shadow-sm">
      {/* Table Toolbar & Filters */}
      <div className="p-4 border-b border-slate-200 flex flex-wrap items-center justify-between gap-3 bg-white">
        <div className="flex items-center flex-wrap gap-2.5 flex-1 min-w-[280px]">
          {/* Search Input */}
          <div className="relative flex-1 min-w-[200px]">
            <Search className="w-4 h-4 text-slate-400 absolute left-3 top-1/2 -translate-y-1/2" />
            <input
              type="text"
              placeholder="搜索 Request ID / Key 别名 / 模型..."
              value={searchKeyword}
              onChange={(e) => onSearchChange(e.target.value)}
              className="w-full bg-slate-50 border border-slate-200 rounded-lg pl-9 pr-3 py-1.5 text-xs text-slate-800 placeholder-slate-400 focus:outline-none focus:border-blue-500 font-medium"
            />
          </div>

          {/* Key Alias Filter */}
          <select
            value={selectedKeyAlias}
            onChange={(e) => onKeyAliasChange(e.target.value)}
            className="bg-slate-50 border border-slate-200 rounded-lg px-2.5 py-1.5 text-xs text-slate-700 focus:outline-none focus:border-blue-500 font-medium"
          >
            <option value="">全部 Key 别名</option>
            <option value="cindy">cindy</option>
            <option value="hebe">hebe</option>
            <option value="rin">rin</option>
            <option value="default_user_id">default_user_id (Master)</option>
          </select>

          {/* Model Filter */}
          <select
            value={selectedModel}
            onChange={(e) => onModelChange(e.target.value)}
            className="bg-slate-50 border border-slate-200 rounded-lg px-2.5 py-1.5 text-xs text-slate-700 focus:outline-none focus:border-blue-500 font-medium"
          >
            <option value="">全部模型</option>
            <option value="gemini-3.7-flash">gemini-3.7-flash</option>
            <option value="gemini-3.7-backup">gemini-3.7-backup</option>
          </select>

          {/* Status Code Filter */}
          <select
            value={selectedStatusCode}
            onChange={(e) => onStatusCodeChange(e.target.value)}
            className="bg-slate-50 border border-slate-200 rounded-lg px-2.5 py-1.5 text-xs text-slate-700 focus:outline-none focus:border-blue-500 font-medium"
          >
            <option value="">全部状态</option>
            <option value="200">200 OK</option>
            <option value="429">429 RateLimit</option>
            <option value="500">500 ServerError</option>
          </select>
        </div>

        {/* Total Count Info */}
        <div className="text-xs text-slate-500 font-medium">
          共 <span className="text-slate-900 font-bold">{logsData?.total ?? 0}</span> 条记录
        </div>
      </div>

      {/* Table Content */}
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead className="bg-slate-50 text-slate-500 uppercase font-semibold border-b border-slate-200">
            <tr>
              <th className="py-3 px-4">触发时间</th>
              <th className="py-3 px-4">Request ID</th>
              <th className="py-3 px-4">调用方 Key</th>
              <th className="py-3 px-4">实际模型 (降级轨迹)</th>
              <th className="py-3 px-4 text-right">消耗 Tokens</th>
              <th className="py-3 px-4 text-right">折合人民币</th>
              <th className="py-3 px-4 text-right">耗时</th>
              <th className="py-3 px-4">状态</th>
              <th className="py-3 px-4 text-center">报文透视</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 text-slate-700">
            {loading && !logsData?.items?.length ? (
              <tr>
                <td colSpan={9} className="py-12 text-center text-slate-400">
                  数据加载中...
                </td>
              </tr>
            ) : !logsData?.items?.length ? (
              <tr>
                <td colSpan={9} className="py-12 text-center text-slate-400">
                  暂无匹配的调用日志
                </td>
              </tr>
            ) : (
              logsData.items.map((log) => {
                const isSelected = selectedLogId === log.request_id;
                return (
                  <tr
                    key={log.id}
                    onClick={() => onSelectLog(log)}
                    className={`cursor-pointer transition-colors hover:bg-slate-50/90 ${
                      isSelected ? "bg-blue-50/80 border-l-2 border-blue-500" : ""
                    }`}
                  >
                    <td className="py-3 px-4 whitespace-nowrap font-mono text-slate-500">
                      {formatTime(log.created_at)}
                    </td>
                    <td className="py-3 px-4 whitespace-nowrap font-mono font-medium">
                      <span className="hover:underline text-blue-600">
                        {log.request_id.length > 20
                          ? log.request_id.slice(0, 10) + "..." + log.request_id.slice(-6)
                          : log.request_id}
                      </span>
                    </td>
                    <td className="py-3 px-4 whitespace-nowrap">
                      <span className="px-2 py-0.5 rounded bg-slate-100 border border-slate-200 text-slate-700 font-semibold">
                        {log.api_key_alias}
                      </span>
                    </td>
                    <td className="py-3 px-4 whitespace-nowrap font-mono">
                      <span className="text-purple-700 font-medium">{log.model_used}</span>
                      {log.model_requested !== log.model_used && (
                        <span className="ml-1.5 text-[10px] text-amber-700 bg-amber-50 px-1 py-0.2 rounded border border-amber-200">
                          降级自 {log.model_requested}
                        </span>
                      )}
                    </td>
                    <td className="py-3 px-4 text-right whitespace-nowrap font-mono font-medium text-slate-800">
                      {log.total_tokens.toLocaleString()}
                      <span className="text-[10px] text-slate-400 ml-1">
                        ({log.prompt_tokens}↑ {log.completion_tokens}↓)
                      </span>
                    </td>
                    <td className="py-3 px-4 text-right whitespace-nowrap font-mono font-semibold text-emerald-600">
                      ¥{log.cost_cny.toFixed(4)}
                    </td>
                    <td className="py-3 px-4 text-right whitespace-nowrap font-mono font-medium text-amber-600">
                      {log.latency_ms}ms
                    </td>
                    <td className="py-3 px-4 whitespace-nowrap">{getStatusBadge(log.status_code)}</td>
                    <td className="py-3 px-4 text-center whitespace-nowrap">
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          onSelectLog(log);
                        }}
                        className="inline-flex items-center gap-1 px-2.5 py-1 rounded bg-slate-100 hover:bg-slate-200 text-blue-600 border border-slate-200 text-xs font-medium transition-all shadow-sm"
                      >
                        <FileText className="w-3.5 h-3.5" /> 查看
                      </button>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      {/* Pagination Bar */}
      {logsData && logsData.total_pages > 1 && (
        <div className="p-3.5 border-t border-slate-200 flex items-center justify-between text-xs text-slate-500 bg-white">
          <div>
            第 <span className="font-semibold text-slate-800">{page}</span> /{" "}
            <span className="font-semibold text-slate-800">{logsData.total_pages}</span> 页
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => onPageChange(page - 1)}
              disabled={page <= 1}
              className="flex items-center gap-1 px-2.5 py-1 rounded bg-slate-50 border border-slate-200 text-slate-700 hover:bg-slate-100 disabled:opacity-40 disabled:cursor-not-allowed font-medium"
            >
              <ChevronLeft className="w-3.5 h-3.5" /> 上一页
            </button>
            <button
              onClick={() => onPageChange(page + 1)}
              disabled={page >= logsData.total_pages}
              className="flex items-center gap-1 px-2.5 py-1 rounded bg-slate-50 border border-slate-200 text-slate-700 hover:bg-slate-100 disabled:opacity-40 disabled:cursor-not-allowed font-medium"
            >
              下一页 <ChevronRight className="w-3.5 h-3.5" />
            </button>
          </div>
        </div>
      )}
    </div>
  );
};
