import React, { useState, useEffect, useCallback } from "react";
import { Navbar } from "./components/Navbar";
import { MetricCards } from "./components/MetricCards";
import { LogsTable } from "./components/LogsTable";
import { PayloadDrawer } from "./components/PayloadDrawer";
import { LogItem, PaginatedLogsResponse, SummaryMetrics } from "./types";

export const App: React.FC = () => {
  const [selectedDate, setSelectedDate] = useState<string>(() => {
    // 默认选用客户端本地/香港时间自然日 (YYYY-MM-DD)，避免 UTC 跨日偏差
    const d = new Date();
    const year = d.getFullYear();
    const month = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  });
  const [autoRefresh, setAutoRefresh] = useState<number>(15);
  const [loading, setLoading] = useState<boolean>(false);

  // Summary Metrics State
  const [metrics, setMetrics] = useState<SummaryMetrics | null>(null);

  // Logs Table State
  const [logsData, setLogsData] = useState<PaginatedLogsResponse | null>(null);
  const [page, setPage] = useState<number>(1);
  const [searchKeyword, setSearchKeyword] = useState<string>("");
  const [selectedKeyAlias, setSelectedKeyAlias] = useState<string>("");
  const [selectedModel, setSelectedModel] = useState<string>("");
  const [selectedStatusCode, setSelectedStatusCode] = useState<string>("");

  // Drawer state
  const [selectedLog, setSelectedLog] = useState<LogItem | null>(null);

  // Fetch metrics (与日志表格保持一致的筛选条件, 确保汇总卡片与记录数对齐)
  const fetchMetrics = useCallback(async () => {
    try {
      const params = new URLSearchParams({ date: selectedDate });
      if (searchKeyword.trim()) {
        params.append("search", searchKeyword.trim());
      }
      if (selectedKeyAlias.trim()) {
        params.append("api_key_alias", selectedKeyAlias.trim());
      }
      if (selectedModel.trim()) {
        params.append("model_used", selectedModel.trim());
      }
      if (selectedStatusCode.trim()) {
        params.append("status_code", selectedStatusCode.trim());
      }
      const res = await fetch(`/api/v1/metrics/summary?${params.toString()}`);
      if (res.ok) {
        const data = await res.json();
        setMetrics(data);
      }
    } catch (err) {
      console.error("Failed to fetch metrics:", err);
    }
  }, [selectedDate, searchKeyword, selectedKeyAlias, selectedModel, selectedStatusCode]);

  // Fetch logs
  const fetchLogs = useCallback(async () => {
    try {
      setLoading(true);
      const params = new URLSearchParams({
        page: page.toString(),
        page_size: "20",
        start_date: selectedDate,
        end_date: selectedDate,
      });

      if (searchKeyword.trim()) {
        params.append("search", searchKeyword.trim());
      }
      if (selectedKeyAlias.trim()) {
        params.append("api_key_alias", selectedKeyAlias.trim());
      }
      if (selectedModel.trim()) {
        params.append("model_used", selectedModel.trim());
      }
      if (selectedStatusCode.trim()) {
        params.append("status_code", selectedStatusCode.trim());
      }

      const res = await fetch(`/api/v1/logs?${params.toString()}`);
      if (res.ok) {
        const data = await res.json();
        setLogsData(data);
      }
    } catch (err) {
      console.error("Failed to fetch logs:", err);
    } finally {
      setLoading(false);
    }
  }, [page, selectedDate, searchKeyword, selectedKeyAlias, selectedModel, selectedStatusCode]);

  // Initial load and auto refresh
  useEffect(() => {
    fetchMetrics();
    fetchLogs();
  }, [fetchMetrics, fetchLogs]);

  useEffect(() => {
    if (autoRefresh <= 0) return;
    const interval = setInterval(() => {
      fetchMetrics();
      fetchLogs();
    }, autoRefresh * 1000);
    return () => clearInterval(interval);
  }, [autoRefresh, fetchMetrics, fetchLogs]);

  const handleManualRefresh = () => {
    fetchMetrics();
    fetchLogs();
  };

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900 flex flex-col">
      {/* Top Navbar */}
      <Navbar
        selectedDate={selectedDate}
        onDateChange={(d) => {
          setSelectedDate(d);
          setPage(1);
        }}
        autoRefresh={autoRefresh}
        onAutoRefreshChange={setAutoRefresh}
        onManualRefresh={handleManualRefresh}
        loading={loading}
      />

      {/* Main Content Area */}
      <main className="flex-1 max-w-7xl w-full mx-auto p-6 space-y-6">
        {/* Metric Cards Banner */}
        <MetricCards
          metrics={metrics}
          loading={loading}
          hasActiveFilters={Boolean(
            searchKeyword.trim() ||
              selectedKeyAlias.trim() ||
              selectedModel.trim() ||
              selectedStatusCode.trim()
          )}
        />

        {/* Audit Logs Table */}
        <LogsTable
          logsData={logsData}
          loading={loading}
          page={page}
          onPageChange={setPage}
          searchKeyword={searchKeyword}
          onSearchChange={(s) => {
            setSearchKeyword(s);
            setPage(1);
          }}
          selectedKeyAlias={selectedKeyAlias}
          onKeyAliasChange={(k) => {
            setSelectedKeyAlias(k);
            setPage(1);
          }}
          selectedModel={selectedModel}
          onModelChange={(m) => {
            setSelectedModel(m);
            setPage(1);
          }}
          selectedStatusCode={selectedStatusCode}
          onStatusCodeChange={(c) => {
            setSelectedStatusCode(c);
            setPage(1);
          }}
          onSelectLog={(log) => setSelectedLog(log)}
          selectedLogId={selectedLog?.request_id ?? null}
        />
      </main>

      {/* Sliding Payload Drawer */}
      <PayloadDrawer log={selectedLog} onClose={() => setSelectedLog(null)} />
    </div>
  );
};
