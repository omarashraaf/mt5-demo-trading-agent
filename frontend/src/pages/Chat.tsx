import { useEffect, useState } from 'react';
import { MessageSquare, Send, DollarSign, Trash2 } from 'lucide-react';
import { api } from '../utils/api';
import type { ChatMessageItem, ChatResponse } from '../types';

interface Props {
  connected: boolean;
}

export default function Chat({ connected }: Props) {
  const initialAssistant: ChatMessageItem = {
    role: 'assistant',
    content: 'Ask me about market context, or request a trade like: BUY XAUUSD 1000',
  };
  const [messages, setMessages] = useState<ChatMessageItem[]>([
    initialAssistant,
  ]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [pendingTradePrompt, setPendingTradePrompt] = useState<string | null>(null);
  const [pendingTradeResponse, setPendingTradeResponse] = useState<ChatResponse | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.getChatHistory()
      .then((res) => {
        if (cancelled) return;
        if (res.messages && res.messages.length > 0) {
          setMessages(res.messages);
        }
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, []);

  const sendMessage = async (message: string, executeTrade: boolean = false) => {
    const trimmed = message.trim();
    if (!trimmed || loading) return;

    setError('');
    if (!executeTrade) {
      setMessages((prev) => [...prev, { role: 'user', content: trimmed }]);
    }

    setLoading(true);
    try {
      const response = await api.sendChatMessage(trimmed, messages, executeTrade);
      setMessages((prev) => [...prev, { role: 'assistant', content: response.reply }]);

      if (response.intent === 'trade_request' && !response.executed) {
        setPendingTradePrompt(trimmed);
        setPendingTradeResponse(response);
      } else {
        setPendingTradePrompt(null);
        setPendingTradeResponse(null);
      }
    } catch (e: any) {
      setError(e.message || 'Failed to send message');
    } finally {
      setLoading(false);
    }
  };

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const message = input;
    setInput('');
    await sendMessage(message, false);
  };

  const executeTrade = async () => {
    if (!pendingTradePrompt) return;
    await sendMessage(pendingTradePrompt, true);
  };

  const clearHistory = async () => {
    setError('');
    try {
      await api.clearChatHistory();
      setMessages([initialAssistant]);
      setPendingTradePrompt(null);
      setPendingTradeResponse(null);
    } catch (e: any) {
      setError(e.message || 'Failed to clear history');
    }
  };

  return (
    <div>
      <div className="page-header">
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
          <h2 style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <MessageSquare size={20} />
            Gemini Chat
          </h2>
          <button className="btn btn-secondary btn-sm" onClick={clearHistory} disabled={loading}>
            <Trash2 size={13} />
            Clear Chat
          </button>
        </div>
        <p>Chat with Gemini for market thinking and request trades from chat.</p>
      </div>

      {!connected && (
        <div className="error-banner mb-4">
          MT5 is not connected. You can still chat, but trade execution requires connection.
        </div>
      )}

      {error && <div className="error-banner mb-4">{error}</div>}

      <div className="card" style={{ minHeight: 420, display: 'flex', flexDirection: 'column' }}>
        <div style={{ flex: 1, overflowY: 'auto', maxHeight: 420, display: 'flex', flexDirection: 'column', gap: 10 }}>
          {messages.map((m, idx) => (
            <div
              key={`${m.role}-${idx}`}
              style={{
                alignSelf: m.role === 'user' ? 'flex-end' : 'flex-start',
                maxWidth: '80%',
                padding: '10px 12px',
                borderRadius: 10,
                background: m.role === 'user' ? 'var(--accent-blue)' : 'var(--bg-secondary)',
                color: m.role === 'user' ? '#fff' : 'var(--text-primary)',
                whiteSpace: 'pre-wrap',
              }}
            >
              {m.content}
            </div>
          ))}
        </div>

        {pendingTradeResponse?.trade_preview && (
          <div className="card mt-3" style={{ background: 'var(--bg-secondary)', border: '1px solid var(--border)' }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
              <div>
                <strong>
                  {pendingTradeResponse.trade_preview.action} {pendingTradeResponse.trade_preview.symbol}
                </strong>
                <div className="text-sm text-muted">
                  ${pendingTradeResponse.trade_preview.amount_usd.toFixed(2)} margin • Vol {pendingTradeResponse.trade_preview.estimated_volume}
                </div>
                <div className="text-sm text-muted">
                  Entry {pendingTradeResponse.trade_preview.estimated_entry} • SL {pendingTradeResponse.trade_preview.stop_loss} • TP {pendingTradeResponse.trade_preview.take_profit}
                </div>
              </div>
              <button className="btn btn-success" onClick={executeTrade} disabled={loading || !connected}>
                <DollarSign size={14} />
                Execute Trade
              </button>
            </div>
          </div>
        )}

        <form onSubmit={onSubmit} className="mt-3" style={{ display: 'flex', gap: 8 }}>
          <input
            className="form-input"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder='Type a message or trade request like "BUY XAUUSD 1000"'
          />
          <button className="btn btn-primary" type="submit" disabled={loading || !input.trim()}>
            <Send size={14} />
            {loading ? 'Sending...' : 'Send'}
          </button>
        </form>
      </div>
    </div>
  );
}
