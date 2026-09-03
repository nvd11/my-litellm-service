import React, { useState, useEffect } from "react";
import {
  X,
  Copy,
  Check,
  ExternalLink,
  Bot,
  User,
  Shield,
  Brain,
  Layers,
  Code2,
  Info,
  Sparkles,
} from "lucide-react";
import { LogItem, PayloadData } from "../types";

interface PayloadDrawerProps {
  log: LogItem | null;
  onClose: () => void;
}

export const PayloadDrawer: React.FC<PayloadDrawerProps> = ({ log, onClose }) => {
  const [activeTab, setActiveTab] = useState<"formatted" | "raw" | "meta">("formatted");
  const [rawTab, setRawTab] = useState<"prompt" | "response">("prompt");
  const [payloadData, setPayloadData] = useState<PayloadData | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [copiedKey, setCopiedKey] = useState<string | null>(null);
  const [expandMessages, setExpandMessages] = useState<boolean>(false);

  useEffect(() => {
    if (!log) {
      setPayloadData(null);
      return;
    }

    const fetchPayload = async () => {
      setLoading(true);
      try {
        const dateStr = log.created_at.split("T")[0];
        const res = await fetch(`/api/v1/logs/${log.request_id}/payload?date=${dateStr}`);
        if (res.ok) {
          const data = await res.json();
          setPayloadData(data);
        } else {
          setPayloadData(null);
        }
      } catch (err) {
        console.error("Failed to load payload from API:", err);
        setPayloadData(null);
      } finally {
        setLoading(false);
      }
    };

    fetchPayload();
  }, [log]);

  const handleCopy = (text: string, key: string) => {
    navigator.clipboard.writeText(text);
    setCopiedKey(key);
    setTimeout(() => setCopiedKey(null), 2000);
  };

  if (!log) return null;

  return (
    <div className="fixed inset-0 z-50 overflow-hidden bg-slate-950/60 backdrop-blur-sm flex justify-end">
      {/* Background click to close */}
      <div className="flex-1" onClick={onClose} />

      {/* Sliding Panel */}
      <div className="w-full max-w-2xl bg-slate-900 border-l border-slate-800 shadow-2xl flex flex-col h-full animate-in slide-in-from-right duration-200">
        {/* Drawer Header */}
        <div className="p-4 border-b border-slate-800 flex items-center justify-between bg-slate-850/70">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-lg bg-blue-500/10 text-blue-400 flex items-center justify-center">
              <Sparkles className="w-4 h-4" />
            </div>
            <div>
              <h2 className="text-sm font-bold text-slate-100 flex items-center gap-2">
                报文深度透视
                <span className="text-xs font-mono px-2 py-0.5 rounded bg-slate-800 text-purple-300 border border-slate-700">
                  {log.model_used}
                </span>
              </h2>
              <p className="text-xs text-slate-400 font-mono">ID: {log.request_id}</p>
            </div>
          </div>

          <div className="flex items-center gap-2">
            <a
              href={log.prompt_url}
              target="_blank"
              rel="noreferrer"
              className="p-1.5 rounded-lg text-slate-400 hover:text-slate-200 hover:bg-slate-800 transition-colors"
              title="在浏览器中直接打开 S3 原始 JSON"
            >
              <ExternalLink className="w-4 h-4" />
            </a>
            <button
              onClick={onClose}
              className="p-1.5 rounded-lg text-slate-400 hover:text-slate-200 hover:bg-slate-800 transition-colors"
            >
              <X className="w-5 h-5" />
            </button>
          </div>
        </div>

        {/* Tab Navigation */}
        <div className="flex border-b border-slate-800 px-4 bg-slate-900 text-xs font-semibold">
          <button
            onClick={() => setActiveTab("formatted")}
            className={`py-3 px-3.5 border-b-2 flex items-center gap-1.5 transition-all ${
              activeTab === "formatted"
                ? "border-blue-500 text-blue-400"
                : "border-transparent text-slate-400 hover:text-slate-200"
            }`}
          >
            <Layers className="w-3.5 h-3.5" /> 结构化视图
          </button>
          <button
            onClick={() => setActiveTab("raw")}
            className={`py-3 px-3.5 border-b-2 flex items-center gap-1.5 transition-all ${
              activeTab === "raw"
                ? "border-blue-500 text-blue-400"
                : "border-transparent text-slate-400 hover:text-slate-200"
            }`}
          >
            <Code2 className="w-3.5 h-3.5" /> 原始 JSON 报文
          </button>
          <button
            onClick={() => setActiveTab("meta")}
            className={`py-3 px-3.5 border-b-2 flex items-center gap-1.5 transition-all ${
              activeTab === "meta"
                ? "border-blue-500 text-blue-400"
                : "border-transparent text-slate-400 hover:text-slate-200"
            }`}
          >
            <Info className="w-3.5 h-3.5" /> 调用元数据
          </button>
        </div>

        {/* Drawer Body */}
        <div className="flex-1 overflow-y-auto p-4 space-y-4 text-xs">
          {loading ? (
            <div className="py-20 text-center text-slate-500 flex flex-col items-center gap-2">
              <div className="w-6 h-6 border-2 border-blue-500 border-t-transparent rounded-full animate-spin" />
              <span>正在从 NUC MinIO 读取原始 Payload...</span>
            </div>
          ) : activeTab === "formatted" ? (
            <div className="space-y-4">
              {/* Card 1: System Prompt */}
              {payloadData?.prompt?.system_prompt && (
                <div className="bg-purple-950/20 border border-purple-800/40 rounded-xl p-3.5 space-y-2">
                  <div className="flex items-center justify-between text-purple-400 font-semibold">
                    <span className="flex items-center gap-1.5">
                      <Shield className="w-3.5 h-3.5" /> System Prompt (人设与系统指令)
                    </span>
                    <button
                      onClick={() =>
                        handleCopy(payloadData.prompt.system_prompt || "", "system")
                      }
                      className="text-slate-400 hover:text-purple-300 flex items-center gap-1"
                    >
                      {copiedKey === "system" ? (
                        <Check className="w-3 h-3 text-emerald-400" />
                      ) : (
                        <Copy className="w-3 h-3" />
                      )}
                      {copiedKey === "system" ? "已复制" : "复制"}
                    </button>
                  </div>
                  <div className="bg-slate-950/70 rounded-lg p-3 text-slate-300 whitespace-pre-wrap leading-relaxed font-sans border border-purple-900/30">
                    {payloadData.prompt.system_prompt}
                  </div>
                </div>
              )}

              {/* Card 2: User Prompt */}
              <div className="bg-blue-950/20 border border-blue-800/40 rounded-xl p-3.5 space-y-2">
                <div className="flex items-center justify-between text-blue-400 font-semibold">
                  <span className="flex items-center gap-1.5">
                    <User className="w-3.5 h-3.5" /> User Prompt (本次最新提问)
                  </span>
                  <button
                    onClick={() =>
                      handleCopy(
                        payloadData?.prompt?.user_prompt || "无用户提示词",
                        "user"
                      )
                    }
                    className="text-slate-400 hover:text-blue-300 flex items-center gap-1"
                  >
                    {copiedKey === "user" ? (
                      <Check className="w-3 h-3 text-emerald-400" />
                    ) : (
                      <Copy className="w-3 h-3" />
                    )}
                    {copiedKey === "user" ? "已复制" : "复制"}
                  </button>
                </div>
                <div className="bg-slate-950/70 rounded-lg p-3 text-slate-200 whitespace-pre-wrap leading-relaxed font-sans border border-blue-900/30 font-medium">
                  {payloadData?.prompt?.user_prompt || "（无最新单独用户输入）"}
                </div>
              </div>

              {/* Card 3: Assistant Reply */}
              <div className="bg-emerald-950/20 border border-emerald-800/40 rounded-xl p-3.5 space-y-2">
                <div className="flex items-center justify-between text-emerald-400 font-semibold">
                  <span className="flex items-center gap-1.5">
                    <Bot className="w-3.5 h-3.5" /> Assistant Reply (模型生成回复)
                  </span>
                  <button
                    onClick={() =>
                      handleCopy(
                        payloadData?.response?.reply || "无模型回复",
                        "reply"
                      )
                    }
                    className="text-slate-400 hover:text-emerald-300 flex items-center gap-1"
                  >
                    {copiedKey === "reply" ? (
                      <Check className="w-3 h-3 text-emerald-400" />
                    ) : (
                      <Copy className="w-3 h-3" />
                    )}
                    {copiedKey === "reply" ? "已复制" : "复制"}
                  </button>
                </div>
                <div className="bg-slate-950/70 rounded-lg p-3 text-slate-100 whitespace-pre-wrap leading-relaxed font-sans border border-emerald-900/30">
                  {payloadData?.response?.reply || (
                    <span className="text-slate-500 italic">
                      {payloadData?.response?.reasoning_content
                        ? "（模型仅输出思考过程，未生成正文）"
                        : "（无文本输出内容）"}
                    </span>
                  )}
                </div>
              </div>

              {/* Card 4: Reasoning Content (if present) */}
              {payloadData?.response?.reasoning_content && (
                <div className="bg-amber-950/20 border border-amber-800/40 rounded-xl p-3.5 space-y-2">
                  <div className="flex items-center justify-between text-amber-400 font-semibold">
                    <span className="flex items-center gap-1.5">
                      <Brain className="w-3.5 h-3.5" /> Reasoning Process (思维链与思考过程)
                    </span>
                    <button
                      onClick={() =>
                        handleCopy(
                          payloadData.response.reasoning_content || "",
                          "reasoning"
                        )
                      }
                      className="text-slate-400 hover:text-amber-300 flex items-center gap-1"
                    >
                      {copiedKey === "reasoning" ? (
                        <Check className="w-3 h-3 text-emerald-400" />
                      ) : (
                        <Copy className="w-3 h-3" />
                      )}
                      {copiedKey === "reasoning" ? "已复制" : "复制"}
                    </button>
                  </div>
                  <div className="bg-slate-950/70 rounded-lg p-3 text-amber-200/90 whitespace-pre-wrap leading-relaxed font-mono text-[11px] border border-amber-900/30 max-h-60 overflow-y-auto">
                    {payloadData.response.reasoning_content}
                  </div>
                </div>
              )}

              {/* Card 5: Full Multi-turn Messages Context Accordion */}
              {payloadData?.prompt?.messages && payloadData.prompt.messages.length > 0 && (
                <div className="border border-slate-800 rounded-xl overflow-hidden">
                  <button
                    onClick={() => setExpandMessages(!expandMessages)}
                    className="w-full bg-slate-850 p-3 flex items-center justify-between text-slate-300 font-semibold hover:bg-slate-800 transition-colors"
                  >
                    <span>
                      多轮对话上下文时序 ({payloadData.prompt.messages.length} 条消息)
                    </span>
                    <span className="text-slate-400 text-[11px]">
                      {expandMessages ? "收起 ▲" : "展开 ▼"}
                    </span>
                  </button>

                  {expandMessages && (
                    <div className="p-3 bg-slate-950/60 divide-y divide-slate-800/60 space-y-2.5">
                      {payloadData.prompt.messages.map((m, idx) => (
                        <div key={idx} className="pt-2 first:pt-0 space-y-1">
                          <div className="flex items-center gap-1.5 font-bold">
                            <span
                              className={`px-1.5 py-0.2 rounded text-[10px] uppercase font-mono ${
                                m.role === "system"
                                  ? "bg-purple-900/30 text-purple-400"
                                  : m.role === "user"
                                  ? "bg-blue-900/30 text-blue-400"
                                  : "bg-emerald-900/30 text-emerald-400"
                              }`}
                            >
                              {m.role}
                            </span>
                          </div>
                          <div className="text-slate-300 whitespace-pre-wrap font-sans pl-1 text-[11px]">
                            {m.content}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          ) : activeTab === "raw" ? (
            <div className="space-y-3">
              {/* Raw Tab Selector */}
              <div className="flex gap-2">
                <button
                  onClick={() => setRawTab("prompt")}
                  className={`px-3 py-1.5 rounded-lg border text-xs font-semibold ${
                    rawTab === "prompt"
                      ? "bg-blue-600 border-blue-500 text-white"
                      : "bg-slate-800 border-slate-700 text-slate-400"
                  }`}
                >
                  📄 prompt.json
                </button>
                <button
                  onClick={() => setRawTab("response")}
                  className={`px-3 py-1.5 rounded-lg border text-xs font-semibold ${
                    rawTab === "response"
                      ? "bg-blue-600 border-blue-500 text-white"
                      : "bg-slate-800 border-slate-700 text-slate-400"
                  }`}
                >
                  📄 response.json
                </button>
              </div>

              {/* Raw JSON Code Viewer */}
              <div className="relative">
                <button
                  onClick={() =>
                    handleCopy(
                      JSON.stringify(
                        rawTab === "prompt" ? payloadData?.prompt : payloadData?.response,
                        null,
                        2
                      ),
                      "raw_json"
                    )
                  }
                  className="absolute right-3 top-3 px-2.5 py-1 rounded bg-slate-800 border border-slate-700 text-slate-300 hover:text-white flex items-center gap-1"
                >
                  {copiedKey === "raw_json" ? (
                    <Check className="w-3 h-3 text-emerald-400" />
                  ) : (
                    <Copy className="w-3 h-3" />
                  )}
                  {copiedKey === "raw_json" ? "已复制" : "复制代码"}
                </button>
                <pre className="bg-slate-950 p-4 rounded-xl border border-slate-800 overflow-x-auto text-[11px] font-mono text-slate-300 leading-relaxed max-h-[550px]">
                  {JSON.stringify(
                    rawTab === "prompt" ? payloadData?.prompt : payloadData?.response,
                    null,
                    2
                  )}
                </pre>
              </div>
            </div>
          ) : (
            /* Metadata Tab */
            <div className="space-y-3 bg-slate-950/60 rounded-xl p-4 border border-slate-800">
              <div className="grid grid-cols-2 gap-3 text-xs">
                <div>
                  <span className="text-slate-500 block">请求时间</span>
                  <span className="font-mono text-slate-200">{log.created_at}</span>
                </div>
                <div>
                  <span className="text-slate-500 block">响应耗时</span>
                  <span className="font-mono text-amber-300">{log.latency_ms} ms</span>
                </div>
                <div>
                  <span className="text-slate-500 block">调用方 Key 别名</span>
                  <span className="font-semibold text-blue-400">{log.api_key_alias}</span>
                </div>
                <div>
                  <span className="text-slate-500 block">上游 Provider Key</span>
                  <span className="font-mono text-slate-300">{log.provider_key_alias}</span>
                </div>
                <div>
                  <span className="text-slate-500 block">请求模型 vs 命中模型</span>
                  <span className="font-mono text-purple-300">
                    {log.model_requested} ➔ {log.model_used}
                  </span>
                </div>
                <div>
                  <span className="text-slate-500 block">结算汇率 (USD/CNY)</span>
                  <span className="font-mono text-slate-200">{log.fx_rate}</span>
                </div>
                <div>
                  <span className="text-slate-500 block">Token 消耗明细</span>
                  <span className="font-mono text-slate-200">
                    总计 {log.total_tokens} (Prompt: {log.prompt_tokens}, Completion: {log.completion_tokens})
                  </span>
                </div>
                <div>
                  <span className="text-slate-500 block">本次扣费 (USD / CNY)</span>
                  <span className="font-mono text-emerald-400 font-bold">
                    ¥{log.cost_cny.toFixed(6)} (${log.cost_usd.toFixed(6)})
                  </span>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
};
