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
  ChevronDown,
  ChevronUp,
  ChevronsUpDown,
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

  // 折叠控制状态：key 为 section 名称或 "msg-{index}"，value 为 true 表示已收起/折叠
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

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

  const toggleCollapse = (key: string) => {
    setCollapsed((prev) => ({
      ...prev,
      [key]: !prev[key],
    }));
  };

  const collapseAll = () => {
    const next: Record<string, boolean> = {
      system: true,
      user: true,
      reply: true,
      reasoning: true,
      messages: true,
    };
    if (payloadData?.prompt?.messages) {
      payloadData.prompt.messages.forEach((_, idx) => {
        next[`msg-${idx}`] = true;
      });
    }
    setCollapsed(next);
  };

  const expandAll = () => {
    setCollapsed({});
  };

  if (!log) return null;

  return (
    <div className="fixed inset-0 z-50 overflow-hidden bg-slate-950/60 backdrop-blur-sm flex justify-end">
      {/* 背景蒙版点击关闭 */}
      <div className="flex-1" onClick={onClose} />

      {/* 右侧滑动面板 */}
      <div className="w-full max-w-2xl bg-slate-900 border-l border-slate-800 shadow-2xl flex flex-col h-full animate-in slide-in-from-right duration-200">
        {/* Drawer 顶栏 */}
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

        {/* Tab 导航与快速折叠按钮 */}
        <div className="flex items-center justify-between border-b border-slate-800 px-4 bg-slate-900 text-xs font-semibold">
          <div className="flex">
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

          {/* 全局全部折叠 / 全部展开快捷按钮 */}
          {activeTab === "formatted" && (
            <div className="flex items-center gap-2 text-[11px] font-normal text-slate-400">
              <button
                onClick={expandAll}
                className="hover:text-blue-400 flex items-center gap-0.5"
              >
                <ChevronDown className="w-3 h-3" /> 全部展开
              </button>
              <span>|</span>
              <button
                onClick={collapseAll}
                className="hover:text-blue-400 flex items-center gap-0.5"
              >
                <ChevronUp className="w-3 h-3" /> 全部收起
              </button>
            </div>
          )}
        </div>

        {/* Drawer 主体区域 */}
        <div className="flex-1 overflow-y-auto p-4 space-y-3.5 text-xs">
          {loading ? (
            <div className="py-20 text-center text-slate-500 flex flex-col items-center gap-2">
              <div className="w-6 h-6 border-2 border-blue-500 border-t-transparent rounded-full animate-spin" />
              <span>正在从 NUC MinIO 读取原始 Payload...</span>
            </div>
          ) : activeTab === "formatted" ? (
            <div className="space-y-3.5">
              {/* 卡片 1: System Prompt (支持独立折叠) */}
              {payloadData?.prompt?.system_prompt && (
                <div className="bg-purple-950/20 border border-purple-800/40 rounded-xl overflow-hidden transition-all">
                  <div
                    onClick={() => toggleCollapse("system")}
                    className="p-3 bg-purple-950/30 flex items-center justify-between text-purple-400 font-semibold cursor-pointer hover:bg-purple-900/20 transition-colors select-none"
                  >
                    <span className="flex items-center gap-1.5">
                      <Shield className="w-3.5 h-3.5" /> System Prompt (人设与系统指令)
                      {collapsed["system"] && (
                        <span className="text-[10px] text-purple-300/70 font-normal ml-2 truncate max-w-[200px]">
                          {payloadData.prompt.system_prompt.slice(0, 30)}...
                        </span>
                      )}
                    </span>
                    <div className="flex items-center gap-2" onClick={(e) => e.stopPropagation()}>
                      <button
                        onClick={() =>
                          handleCopy(payloadData.prompt.system_prompt || "", "system")
                        }
                        className="text-slate-400 hover:text-purple-300 flex items-center gap-1 p-1 rounded"
                      >
                        {copiedKey === "system" ? (
                          <Check className="w-3 h-3 text-emerald-400" />
                        ) : (
                          <Copy className="w-3 h-3" />
                        )}
                        {copiedKey === "system" ? "已复制" : "复制"}
                      </button>
                      <button
                        onClick={() => toggleCollapse("system")}
                        className="text-purple-400 hover:text-purple-200 p-1"
                      >
                        {collapsed["system"] ? (
                          <ChevronDown className="w-4 h-4" />
                        ) : (
                          <ChevronUp className="w-4 h-4" />
                        )}
                      </button>
                    </div>
                  </div>
                  {!collapsed["system"] && (
                    <div className="p-3 bg-slate-950/70 text-slate-300 whitespace-pre-wrap leading-relaxed font-sans border-t border-purple-900/30">
                      {payloadData.prompt.system_prompt}
                    </div>
                  )}
                </div>
              )}

              {/* 卡片 2: User Prompt (支持独立折叠) */}
              <div className="bg-blue-950/20 border border-blue-800/40 rounded-xl overflow-hidden transition-all">
                <div
                  onClick={() => toggleCollapse("user")}
                  className="p-3 bg-blue-950/30 flex items-center justify-between text-blue-400 font-semibold cursor-pointer hover:bg-blue-900/20 transition-colors select-none"
                >
                  <span className="flex items-center gap-1.5">
                    <User className="w-3.5 h-3.5" /> User Prompt (本次最新提问)
                    {collapsed["user"] && payloadData?.prompt?.user_prompt && (
                      <span className="text-[10px] text-blue-300/70 font-normal ml-2 truncate max-w-[200px]">
                        {payloadData.prompt.user_prompt.slice(0, 30)}...
                      </span>
                    )}
                  </span>
                  <div className="flex items-center gap-2" onClick={(e) => e.stopPropagation()}>
                    <button
                      onClick={() =>
                        handleCopy(
                          payloadData?.prompt?.user_prompt || "无用户提示词",
                          "user"
                        )
                      }
                      className="text-slate-400 hover:text-blue-300 flex items-center gap-1 p-1 rounded"
                    >
                      {copiedKey === "user" ? (
                        <Check className="w-3 h-3 text-emerald-400" />
                      ) : (
                        <Copy className="w-3 h-3" />
                      )}
                      {copiedKey === "user" ? "已复制" : "复制"}
                    </button>
                    <button
                      onClick={() => toggleCollapse("user")}
                      className="text-blue-400 hover:text-blue-200 p-1"
                    >
                      {collapsed["user"] ? (
                        <ChevronDown className="w-4 h-4" />
                      ) : (
                        <ChevronUp className="w-4 h-4" />
                      )}
                    </button>
                  </div>
                </div>
                {!collapsed["user"] && (
                  <div className="p-3 bg-slate-950/70 text-slate-200 whitespace-pre-wrap leading-relaxed font-sans border-t border-blue-900/30 font-medium">
                    {payloadData?.prompt?.user_prompt || "（无最新单独用户输入）"}
                  </div>
                )}
              </div>

              {/* 卡片 3: Assistant Reply (支持独立折叠) */}
              <div className="bg-emerald-950/20 border border-emerald-800/40 rounded-xl overflow-hidden transition-all">
                <div
                  onClick={() => toggleCollapse("reply")}
                  className="p-3 bg-emerald-950/30 flex items-center justify-between text-emerald-400 font-semibold cursor-pointer hover:bg-emerald-900/20 transition-colors select-none"
                >
                  <span className="flex items-center gap-1.5">
                    <Bot className="w-3.5 h-3.5" /> Assistant Reply (模型生成回复)
                    {collapsed["reply"] && payloadData?.response?.reply && (
                      <span className="text-[10px] text-emerald-300/70 font-normal ml-2 truncate max-w-[200px]">
                        {payloadData.response.reply.slice(0, 30)}...
                      </span>
                    )}
                  </span>
                  <div className="flex items-center gap-2" onClick={(e) => e.stopPropagation()}>
                    <button
                      onClick={() =>
                        handleCopy(
                          payloadData?.response?.reply || "无模型回复",
                          "reply"
                        )
                      }
                      className="text-slate-400 hover:text-emerald-300 flex items-center gap-1 p-1 rounded"
                    >
                      {copiedKey === "reply" ? (
                        <Check className="w-3 h-3 text-emerald-400" />
                      ) : (
                        <Copy className="w-3 h-3" />
                      )}
                      {copiedKey === "reply" ? "已复制" : "复制"}
                    </button>
                    <button
                      onClick={() => toggleCollapse("reply")}
                      className="text-emerald-400 hover:text-emerald-200 p-1"
                    >
                      {collapsed["reply"] ? (
                        <ChevronDown className="w-4 h-4" />
                      ) : (
                        <ChevronUp className="w-4 h-4" />
                      )}
                    </button>
                  </div>
                </div>
                {!collapsed["reply"] && (
                  <div className="p-3 bg-slate-950/70 text-slate-100 whitespace-pre-wrap leading-relaxed font-sans border-t border-emerald-900/30">
                    {payloadData?.response?.reply || (
                      <span className="text-slate-500 italic">
                        {payloadData?.response?.reasoning_content
                          ? "（模型仅输出思考过程，未生成正文）"
                          : "（无文本输出内容）"}
                      </span>
                    )}
                  </div>
                )}
              </div>

              {/* 卡片 4: Reasoning Content 思维链 (支持独立折叠) */}
              {payloadData?.response?.reasoning_content && (
                <div className="bg-amber-950/20 border border-amber-800/40 rounded-xl overflow-hidden transition-all">
                  <div
                    onClick={() => toggleCollapse("reasoning")}
                    className="p-3 bg-amber-950/30 flex items-center justify-between text-amber-400 font-semibold cursor-pointer hover:bg-amber-900/20 transition-colors select-none"
                  >
                    <span className="flex items-center gap-1.5">
                      <Brain className="w-3.5 h-3.5" /> Reasoning Process (思维链与思考过程)
                      {collapsed["reasoning"] && (
                        <span className="text-[10px] text-amber-300/70 font-normal ml-2 truncate max-w-[200px]">
                          {payloadData.response.reasoning_content.slice(0, 30)}...
                        </span>
                      )}
                    </span>
                    <div className="flex items-center gap-2" onClick={(e) => e.stopPropagation()}>
                      <button
                        onClick={() =>
                          handleCopy(
                            payloadData.response.reasoning_content || "",
                            "reasoning"
                          )
                        }
                        className="text-slate-400 hover:text-amber-300 flex items-center gap-1 p-1 rounded"
                      >
                        {copiedKey === "reasoning" ? (
                          <Check className="w-3 h-3 text-emerald-400" />
                        ) : (
                          <Copy className="w-3 h-3" />
                        )}
                        {copiedKey === "reasoning" ? "已复制" : "复制"}
                      </button>
                      <button
                        onClick={() => toggleCollapse("reasoning")}
                        className="text-amber-400 hover:text-amber-200 p-1"
                      >
                        {collapsed["reasoning"] ? (
                          <ChevronDown className="w-4 h-4" />
                        ) : (
                          <ChevronUp className="w-4 h-4" />
                        )}
                      </button>
                    </div>
                  </div>
                  {!collapsed["reasoning"] && (
                    <div className="p-3 bg-slate-950/70 text-amber-200/90 whitespace-pre-wrap leading-relaxed font-mono text-[11px] border-t border-amber-900/30 max-h-60 overflow-y-auto">
                      {payloadData.response.reasoning_content}
                    </div>
                  )}
                </div>
              )}

              {/* 卡片 5: 多轮对话上下文时序 (支持整卡折叠 + 每条消息单项折叠) */}
              {payloadData?.prompt?.messages && payloadData.prompt.messages.length > 0 && (
                <div className="border border-slate-800 rounded-xl overflow-hidden bg-slate-900">
                  <div
                    onClick={() => toggleCollapse("messages")}
                    className="p-3 bg-slate-850 flex items-center justify-between text-slate-300 font-semibold cursor-pointer hover:bg-slate-800 transition-colors select-none"
                  >
                    <span className="flex items-center gap-1.5">
                      <ChevronsUpDown className="w-3.5 h-3.5 text-blue-400" />
                      多轮对话上下文列表 ({payloadData.prompt.messages.length} 条消息)
                    </span>
                    <span className="text-slate-400 text-[11px] flex items-center gap-1">
                      {collapsed["messages"] ? "展开 ▼" : "收起 ▲"}
                    </span>
                  </div>

                  {!collapsed["messages"] && (
                    <div className="p-3 bg-slate-950/60 divide-y divide-slate-800/60 space-y-2.5">
                      {payloadData.prompt.messages.map((m, idx) => {
                        const msgKey = `msg-${idx}`;
                        const isMsgCollapsed = collapsed[msgKey];
                        return (
                          <div key={idx} className="pt-2 first:pt-0 space-y-1">
                            {/* 单条消息头：支持独立点击折叠 */}
                            <div
                              onClick={() => toggleCollapse(msgKey)}
                              className="flex items-center justify-between cursor-pointer group py-0.5"
                            >
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
                                <span className="text-[10px] text-slate-500 font-mono">
                                  #{idx + 1}
                                </span>
                                {isMsgCollapsed && (
                                  <span className="text-[10px] text-slate-400 font-normal truncate max-w-[280px]">
                                    {(m.content || "").slice(0, 40)}...
                                  </span>
                                )}
                              </div>
                              <div className="flex items-center gap-1.5 text-slate-500 group-hover:text-slate-300">
                                <button
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    handleCopy(m.content || "", msgKey);
                                  }}
                                  className="hover:text-blue-400 p-0.5"
                                  title="复制此条"
                                >
                                  {copiedKey === msgKey ? (
                                    <Check className="w-3 h-3 text-emerald-400" />
                                  ) : (
                                    <Copy className="w-3 h-3" />
                                  )}
                                </button>
                                {isMsgCollapsed ? (
                                  <ChevronDown className="w-3.5 h-3.5" />
                                ) : (
                                  <ChevronUp className="w-3.5 h-3.5" />
                                )}
                              </div>
                            </div>

                            {/* 单条消息正文 */}
                            {!isMsgCollapsed && (
                              <div className="text-slate-300 whitespace-pre-wrap font-sans pl-1.5 text-[11px] leading-relaxed bg-slate-900/50 p-2 rounded-lg border border-slate-850">
                                {m.content}
                              </div>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>
              )}
            </div>
          ) : activeTab === "raw" ? (
            <div className="space-y-3">
              {/* Raw Tab 切换 */}
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

              {/* Raw JSON 展示区 */}
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
            /* 元数据 Tab */
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
