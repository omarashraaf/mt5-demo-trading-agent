import { useState, useEffect, useCallback, useRef } from 'react';
import { RefreshCw, AlertOctagon, X, TrendingUp, TrendingDown, Minus, Play, HelpCircle, DollarSign, ShieldCheck, Clock, Zap, Power, CheckCircle, XCircle } from 'lucide-react';
import { api } from '../utils/api';
import type { StatusResponse, PositionInfo, Recommendation, OrderResult } from '../types';
import ConnectionBar from '../components/ConnectionBar';
import { getSymbolName, getSymbolEmoji, getSignalStrength, getActionDescription, explainProfitLoss } from '../utils/symbolNames';

interface Props {
  status: StatusResponse | null;
  onRefresh: () => void;
}

// Beginner-friendly recommendation card
function TradeCard({ rec, onExecuted, currency }: { rec: Recommendation; onExecuted: () => void; currency: string }) {
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<OrderResult | null>(null);
  const [dismissed, setDismissed] = useState(false);
  const [showDetails, setShowDetails] = useState(false);
  const [showAmountInput, setShowAmountInput] = useState(false);
  const [amount, setAmount] = useState('');
  const [volumePreview, setVolumePreview] = useState<{ volume: number; actual_cost: number; contract_size: number; price: number; min_sl_tp_dollars: number } | null>(null);
  // SL/TP as dollar amounts: how much you're willing to lose / want to gain
  const [slDollar, setSlDollar] = useState('');
  const [tpDollar, setTpDollar] = useState('');

  if (dismissed) return null;

  const digits = rec.entry_price_estimate < 50 ? 5 : 2;
  const presetAmounts = [10, 25, 50, 100, 250];
  const minSlTp = volumePreview?.min_sl_tp_dollars || 0;

  // Convert dollar SL/TP to price levels for execution
  const dollarToPrice = (dollarAmount: number, isSL: boolean) => {
    if (!volumePreview || volumePreview.volume <= 0 || volumePreview.contract_size <= 0) return 0;
    const pricePerUnit = volumePreview.volume * volumePreview.contract_size;
    const livePrice = ask || volumePreview.price || rec.entry_price_estimate || 0;
    if (livePrice <= 0) return 0;
    return isSL ? livePrice - dollarAmount / pricePerUnit : livePrice + dollarAmount / pricePerUnit;
  };

  const handleAmountChange = async (val: string) => {
    setAmount(val);
    setVolumePreview(null);
    const num = parseFloat(val);
    if (num > 0) {
      try {
        const preview = await api.calculateVolume(rec.symbol, num);
        setVolumePreview(preview);
        // Default SL/TP = 10% of investment, but at least the minimum required
        const defaultSlTp = Math.max(Math.round(num * 0.10), Math.ceil(preview.min_sl_tp_dollars));
        setSlDollar(String(defaultSlTp));
        setTpDollar(String(defaultSlTp));
      } catch {}
    } else {
      setSlDollar('');
      setTpDollar('');
    }
  };

  const handleExecute = async () => {
    if (!rec.signal_id) return;
    const amountNum = parseFloat(amount);
    if (!amountNum || amountNum <= 0) return;
    // Convert dollar SL/TP to price levels
    const slAmt = parseFloat(slDollar) || 0;
    const tpAmt = parseFloat(tpDollar) || 0;
    const slPrice = slAmt > 0 ? dollarToPrice(slAmt, true) : undefined;
    const tpPrice = tpAmt > 0 ? dollarToPrice(tpAmt, false) : undefined;
    setLoading(true);
    try {
      const res = await api.executeRecommendation(rec.signal_id, amountNum, slPrice, tpPrice);
      setResult(res);
      setShowAmountInput(false);
      onExecuted();
    } catch (e: any) {
      setResult({
        success: false, retcode: -1, retcode_desc: e.message,
        ticket: null, volume: null, price: null,
        stop_loss: null, take_profit: null, comment: '',
      });
    } finally {
      setLoading(false);
    }
  };

  // Live tick data
  const [bid, setBid] = useState<number | null>(null);
  const [ask, setAsk] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    const fetchTick = async () => {
      try {
        const tick = await api.getTick(rec.symbol);
        if (!cancelled) { setBid(tick.bid); setAsk(tick.ask); }
      } catch {}
    };
    fetchTick();
    const t = setInterval(fetchTick, 15000);
    return () => { cancelled = true; clearInterval(t); };
  }, [rec.symbol]);

  const isBuy = rec.signal.action === 'BUY';
  const isSell = rec.signal.action === 'SELL';
  const isHold = rec.signal.action === 'HOLD';
  const strength = getSignalStrength(rec.signal.confidence);
  const symbolName = getSymbolName(rec.symbol);
  const emoji = getSymbolEmoji(rec.symbol);

  if (isHold) {
    return (
      <div className="card" style={{ padding: '16px 20px', opacity: 0.6, borderLeft: '3px solid var(--border)' }}>
        <div className="flex items-center gap-3">
          <span style={{ fontSize: 20 }}>{emoji}</span>
          <div>
            <div style={{ fontWeight: 600, fontSize: 14 }}>{symbolName}</div>
            <div className="text-muted" style={{ fontSize: 12 }}>
              {rec.explanation || 'No good opportunity right now - waiting'}
            </div>
          </div>
        </div>
      </div>
    );
  }

  const borderColor = isBuy ? 'var(--accent-green)' : 'var(--accent-red)';
  const actionWord = isBuy ? 'Buy' : 'Sell';
  const actionIcon = isBuy ? <TrendingUp size={16} /> : <TrendingDown size={16} />;
  const actionExplain = isBuy
    ? 'The AI thinks this price will go UP. You buy now and sell later when the price is higher to make a profit.'
    : 'The AI thinks this price will go DOWN. You sell now and buy back later when the price is lower to make a profit.';

  return (
    <div className="card" style={{ borderLeft: `4px solid ${borderColor}`, padding: '20px 24px' }}>
      {/* Header */}
      <div className="flex justify-between items-center mb-3">
        <div className="flex items-center gap-3">
          <span style={{ fontSize: 24 }}>{emoji}</span>
          <div>
            <div style={{ fontSize: 16, fontWeight: 700 }}>{symbolName}</div>
            <div className="text-muted" style={{ fontSize: 12 }}>{rec.symbol}</div>
          </div>
        </div>
        {/* Live Prices */}
        <div style={{ textAlign: 'center', fontFamily: 'monospace' }}>
          {bid !== null && ask !== null ? (
            <div className="flex gap-4">
              <div>
                <div style={{ fontSize: 9, color: 'var(--accent-red)', fontWeight: 700 }}>SELL</div>
                <div style={{ fontSize: 14, fontWeight: 600 }}>{bid.toFixed(digits)}</div>
              </div>
              <div>
                <div style={{ fontSize: 9, color: 'var(--accent-green)', fontWeight: 700 }}>BUY</div>
                <div style={{ fontSize: 14, fontWeight: 600 }}>{ask.toFixed(digits)}</div>
              </div>
            </div>
          ) : (
            <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>Loading prices...</span>
          )}
        </div>
        <div style={{ textAlign: 'right' }}>
          <div className={`badge ${isBuy ? 'badge-green' : 'badge-red'}`} style={{ fontSize: 14, padding: '5px 16px' }}>
            {actionIcon}
            <span style={{ marginLeft: 4 }}>{actionWord}</span>
          </div>
          <div style={{ fontSize: 11, marginTop: 4, color: strength.color, fontWeight: 600 }}>
            {strength.label} Signal
          </div>
        </div>
      </div>

      {/* Simple explanation */}
      <div style={{
        background: 'var(--bg-tertiary)',
        borderRadius: 8,
        padding: '12px 16px',
        marginBottom: 16,
        fontSize: 13,
        lineHeight: 1.6,
        color: 'var(--text-secondary)',
      }}>
        {rec.explanation}
      </div>

      {/* What this means for you */}
      <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 12, display: 'flex', alignItems: 'center', gap: 6 }}>
        <HelpCircle size={12} />
        {actionExplain}
      </div>

      {/* Safety info */}
      <div className="flex gap-3 mb-3" style={{ fontSize: 11 }}>
        <div className="flex items-center gap-2" style={{ color: 'var(--accent-green)' }}>
          <ShieldCheck size={12} />
          Stop-loss protection included
        </div>
        <div className="flex items-center gap-2 text-muted">
          <DollarSign size={12} />
          Risk managed automatically
        </div>
        {rec.signal.max_holding_minutes && (
          <div className="flex items-center gap-2 text-muted">
            <Clock size={12} />
            ~{rec.signal.max_holding_minutes}min hold
          </div>
        )}
      </div>

      {/* Expandable details */}
      <button
        className="btn btn-secondary btn-sm mb-3"
        onClick={() => setShowDetails(!showDetails)}
        style={{ fontSize: 11, opacity: 0.6 }}
      >
        {showDetails ? 'Hide' : 'Show'} technical details
      </button>

      {showDetails && (
        <div className="grid-3 gap-3 mb-3" style={{ fontSize: 12, background: 'var(--bg-primary)', padding: 12, borderRadius: 6 }}>
          <div>
            <span className="text-muted">Entry Price </span>
            <span className="font-mono">{rec.entry_price_estimate.toFixed(rec.entry_price_estimate < 50 ? 5 : 2)}</span>
          </div>
          <div>
            <span className="text-muted">Stop Loss </span>
            <span className="font-mono text-red">{rec.signal.stop_loss?.toFixed(rec.entry_price_estimate < 50 ? 5 : 2) ?? '-'}</span>
          </div>
          <div>
            <span className="text-muted">Take Profit </span>
            <span className="font-mono text-green">{rec.signal.take_profit?.toFixed(rec.entry_price_estimate < 50 ? 5 : 2) ?? '-'}</span>
          </div>
        </div>
      )}

      {/* Risk warnings (alerts, not blocks) */}
      {rec.risk_decision?.warnings && rec.risk_decision.warnings.length > 0 && (
        <div style={{
          background: 'rgba(234, 179, 8, 0.08)',
          border: '1px solid rgba(234, 179, 8, 0.2)',
          borderRadius: 6,
          padding: '10px 14px',
          fontSize: 12,
          color: 'var(--accent-yellow)',
          marginBottom: 12,
        }}>
          {rec.risk_decision.warnings.map((w, i) => (
            <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: i < rec.risk_decision.warnings.length - 1 ? 4 : 0 }}>
              <span style={{ fontSize: 10 }}>&#9888;</span> {w}
            </div>
          ))}
        </div>
      )}

      {/* Result */}
      {result ? (
        <div style={{
          background: result.success ? 'rgba(34, 197, 94, 0.08)' : 'rgba(239, 68, 68, 0.08)',
          border: `1px solid ${result.success ? 'rgba(34, 197, 94, 0.3)' : 'rgba(239, 68, 68, 0.3)'}`,
          borderRadius: 8,
          padding: '14px 18px',
          fontSize: 13,
          color: result.success ? 'var(--accent-green)' : 'var(--accent-red)',
        }}>
          {result.success
            ? `Trade placed successfully! Order #${result.ticket}`
            : `Could not place trade: ${result.retcode_desc}`
          }
        </div>
      ) : !showAmountInput ? (
        <div className="flex gap-3">
          <button
            className="btn btn-success"
            onClick={() => {
              setShowAmountInput(true);
            }}
            style={{ fontSize: 15, padding: '12px 28px', fontWeight: 600 }}
          >
            <Play size={16} />
            {actionWord} Now
          </button>
          <button
            className="btn btn-secondary"
            onClick={() => setDismissed(true)}
            style={{ fontSize: 13, padding: '12px 20px' }}
          >
            Skip
          </button>
        </div>
      ) : (
        <div style={{
          background: 'var(--bg-tertiary)',
          borderRadius: 10,
          padding: '16px 20px',
          border: `1px solid ${borderColor}`,
        }}>
          <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 12 }}>
            How much do you want to invest? ({currency})
          </div>

          {/* Preset amounts */}
          <div className="flex gap-2" style={{ marginBottom: 12, flexWrap: 'wrap' }}>
            {presetAmounts.map((preset) => (
              <button
                key={preset}
                className={`btn ${amount === String(preset) ? 'btn-success' : 'btn-secondary'}`}
                onClick={() => handleAmountChange(String(preset))}
                style={{ fontSize: 13, padding: '8px 16px', fontWeight: 600 }}
              >
                ${preset}
              </button>
            ))}
          </div>

          {/* Custom amount */}
          <div className="flex items-center gap-3" style={{ marginBottom: 12 }}>
            <div style={{ position: 'relative', flex: 1 }}>
              <span style={{
                position: 'absolute', left: 12, top: '50%', transform: 'translateY(-50%)',
                color: 'var(--text-muted)', fontSize: 15, fontWeight: 600,
              }}>$</span>
              <input
                className="form-input"
                type="number"
                placeholder="Enter amount"
                value={amount}
                onChange={(e) => handleAmountChange(e.target.value)}
                style={{
                  fontSize: 18, padding: '12px 12px 12px 28px', fontWeight: 600,
                  fontFamily: 'monospace', textAlign: 'right',
                }}
                min="1"
                autoFocus
              />
            </div>
          </div>

          {/* Volume preview */}
          {volumePreview && (
            <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 12 }}>
              This will trade <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{volumePreview.volume} lots</span>
              {' '}(~${volumePreview.actual_cost.toLocaleString()} market value)
            </div>
          )}

          {/* Stop Loss & Take Profit - dollar based */}
          {volumePreview && (
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, marginBottom: 12 }}>
            <div>
              <label style={{ fontSize: 11, color: 'var(--accent-red)', fontWeight: 600, display: 'block', marginBottom: 4 }}>
                Max Loss (auto-sell if you lose this much)
              </label>
              <div style={{ position: 'relative' }}>
                <span style={{ position: 'absolute', left: 10, top: '50%', transform: 'translateY(-50%)', color: 'var(--accent-red)', fontSize: 14, fontWeight: 600 }}>$</span>
                <input
                  className="form-input"
                  type="number"
                  value={slDollar}
                  onChange={(e) => setSlDollar(e.target.value)}
                  onClick={(e) => e.stopPropagation()}
                  style={{ fontSize: 14, padding: '8px 12px 8px 24px', fontFamily: 'monospace', borderColor: parseFloat(slDollar) > 0 && parseFloat(slDollar) < minSlTp ? 'rgba(239,68,68,0.8)' : 'rgba(239,68,68,0.3)' }}
                  step="1" min={Math.ceil(minSlTp)}
                />
              </div>
              {parseFloat(slDollar) > 0 && parseFloat(slDollar) < minSlTp && (
                <div style={{ fontSize: 10, color: '#ef4444', marginTop: 3, fontWeight: 600 }}>
                  ⚠ Minimum ${Math.ceil(minSlTp)} required (broker limit)
                </div>
              )}
              {slDollar && parseFloat(slDollar) >= minSlTp && parseFloat(amount) > 0 && (
                <div style={{ fontSize: 10, color: 'var(--accent-red)', marginTop: 3 }}>
                  Your ${amount} drops to <span style={{ fontWeight: 700 }}>${(parseFloat(amount) - parseFloat(slDollar)).toFixed(0)}</span>
                  {' '}({((parseFloat(slDollar) / parseFloat(amount)) * 100).toFixed(0)}% loss)
                  {dollarToPrice(parseFloat(slDollar), true) > 0 && (
                    <span style={{ opacity: 0.7 }}> · price: {dollarToPrice(parseFloat(slDollar), true).toFixed(digits)}</span>
                  )}
                </div>
              )}
            </div>
            <div>
              <label style={{ fontSize: 11, color: 'var(--accent-green)', fontWeight: 600, display: 'block', marginBottom: 4 }}>
                Target Profit (auto-sell when you gain this much)
              </label>
              <div style={{ position: 'relative' }}>
                <span style={{ position: 'absolute', left: 10, top: '50%', transform: 'translateY(-50%)', color: 'var(--accent-green)', fontSize: 14, fontWeight: 600 }}>$</span>
                <input
                  className="form-input"
                  type="number"
                  value={tpDollar}
                  onChange={(e) => setTpDollar(e.target.value)}
                  onClick={(e) => e.stopPropagation()}
                  style={{ fontSize: 14, padding: '8px 12px 8px 24px', fontFamily: 'monospace', borderColor: parseFloat(tpDollar) > 0 && parseFloat(tpDollar) < minSlTp ? 'rgba(34,197,94,0.8)' : 'rgba(34,197,94,0.3)' }}
                  step="1" min={Math.ceil(minSlTp)}
                />
              </div>
              {parseFloat(tpDollar) > 0 && parseFloat(tpDollar) < minSlTp && (
                <div style={{ fontSize: 10, color: '#ef4444', marginTop: 3, fontWeight: 600 }}>
                  ⚠ Minimum ${Math.ceil(minSlTp)} required (broker limit)
                </div>
              )}
              {tpDollar && parseFloat(tpDollar) >= minSlTp && parseFloat(amount) > 0 && (
                <div style={{ fontSize: 10, color: 'var(--accent-green)', marginTop: 3 }}>
                  Your ${amount} grows to <span style={{ fontWeight: 700 }}>${(parseFloat(amount) + parseFloat(tpDollar)).toFixed(0)}</span>
                  {' '}({((parseFloat(tpDollar) / parseFloat(amount)) * 100).toFixed(0)}% gain)
                  {dollarToPrice(parseFloat(tpDollar), false) > 0 && (
                    <span style={{ opacity: 0.7 }}> · price: {dollarToPrice(parseFloat(tpDollar), false).toFixed(digits)}</span>
                  )}
                </div>
              )}
            </div>
          </div>
          )}

          {/* Execute + Cancel */}
          <div className="flex gap-2">
            <button
              className="btn btn-success"
              onClick={handleExecute}
              disabled={loading || !amount || parseFloat(amount) <= 0 || (parseFloat(slDollar) > 0 && parseFloat(slDollar) < minSlTp) || (parseFloat(tpDollar) > 0 && parseFloat(tpDollar) < minSlTp)}
              style={{ fontSize: 14, padding: '10px 24px', fontWeight: 600 }}
            >
              {loading ? <span className="loading-spinner" /> : <Play size={14} />}
              Confirm Buy {amount ? `$${amount}` : ''}
            </button>
            <button
              className="btn btn-secondary"
              onClick={() => { setShowAmountInput(false); setAmount(''); setVolumePreview(null); setSlDollar(''); setTpDollar(''); }}
              style={{ fontSize: 13, padding: '10px 16px' }}
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// Beginner-friendly position card
function PositionCard({ position, currency, onClose, leverage = 100 }: { position: PositionInfo; currency: string; onClose: (ticket: number) => void; leverage?: number }) {
  const [closing, setClosing] = useState(false);
  const [contractSize, setContractSize] = useState<number | null>(null);
  const symbolName = getSymbolName(position.symbol);
  const emoji = getSymbolEmoji(position.symbol);
  const isBuy = position.type === 'BUY';
  const profitColor = position.profit >= 0 ? 'var(--accent-green)' : 'var(--accent-red)';
  const profitText = explainProfitLoss(position.profit, currency);
  const digs = position.price_open < 50 ? 5 : 2;

  // Fetch contract size for dollar SL/TP calculation
  useEffect(() => {
    api.getSymbolInfo(position.symbol).then((info: any) => {
      if (info?.trade_contract_size) setContractSize(info.trade_contract_size);
    }).catch(() => {});
  }, [position.symbol]);

  // Calculate dollar SL/TP amounts
  const calcDollarDistance = (priceLevel: number) => {
    if (!contractSize || priceLevel <= 0) return null;
    const distance = Math.abs(position.price_open - priceLevel);
    return distance * position.volume * contractSize;
  };
  const slDollars = position.stop_loss > 0 ? calcDollarDistance(position.stop_loss) : null;
  const tpDollars = position.take_profit > 0 ? calcDollarDistance(position.take_profit) : null;

  // Calculate investment amount — use comment if available, otherwise compute from position data
  const commentAmount = position.comment?.match(/TA:\$(\d+)/)?.[1];
  const computedInvestment = contractSize
    ? Math.round(position.volume * position.price_open * contractSize / leverage)
    : 0;
  const investedNum = commentAmount ? parseFloat(commentAmount) : computedInvestment;
  const currentValue = investedNum > 0 ? investedNum + position.profit : 0;

  const handleClose = async () => {
    setClosing(true);
    try {
      await onClose(position.ticket);
    } finally {
      setClosing(false);
    }
  };

  // Time since opened
  const openTime = new Date(position.time * 1000);
  const minutesOpen = Math.round((Date.now() - openTime.getTime()) / 60000);
  const timeStr = minutesOpen < 60 ? `${minutesOpen}m ago` : `${Math.round(minutesOpen / 60)}h ${minutesOpen % 60}m ago`;

  return (
    <div className="card" style={{
      borderLeft: `3px solid ${profitColor}`,
      padding: '16px 20px',
    }}>
      {/* Header row */}
      <div className="flex justify-between items-center">
        <div className="flex items-center gap-3">
          <span style={{ fontSize: 20 }}>{emoji}</span>
          <div>
            <div style={{ fontWeight: 600, fontSize: 14 }}>{symbolName}</div>
            <div className="flex items-center gap-2" style={{ fontSize: 12, marginTop: 2 }}>
              <span className={`badge ${isBuy ? 'badge-green' : 'badge-red'}`} style={{ fontSize: 10 }}>
                {position.type}
              </span>
              <span className="text-muted">Opened {timeStr}</span>
            </div>
          </div>
        </div>
        <div style={{ textAlign: 'right' }}>
          <div style={{ fontSize: 18, fontWeight: 700, color: profitColor, fontFamily: 'monospace' }}>
            {position.profit >= 0 ? '+' : ''}{position.profit.toFixed(2)} {currency}
          </div>
          <div className="text-muted" style={{ fontSize: 11 }}>{profitText}</div>
        </div>
      </div>

      {/* Investment summary - big numbers */}
      {investedNum > 0 && (
        <div style={{
          display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 8, marginTop: 10,
          background: 'var(--bg-tertiary)', borderRadius: 8, padding: '10px 14px',
        }}>
          <div style={{ textAlign: 'center' }}>
            <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 2 }}>Started With</div>
            <div style={{ fontSize: 16, fontWeight: 700, fontFamily: 'monospace' }}>${investedNum}</div>
          </div>
          <div style={{ textAlign: 'center' }}>
            <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 2 }}>Current Value</div>
            <div style={{ fontSize: 16, fontWeight: 700, fontFamily: 'monospace', color: profitColor }}>
              ${currentValue.toFixed(2)}
            </div>
          </div>
          <div style={{ textAlign: 'center' }}>
            <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 2 }}>Return</div>
            <div style={{ fontSize: 16, fontWeight: 700, fontFamily: 'monospace', color: profitColor }}>
              {position.profit >= 0 ? '+' : ''}{investedNum > 0 ? ((position.profit / investedNum) * 100).toFixed(1) : '0'}%
            </div>
          </div>
        </div>
      )}

      {/* Trade details row */}
      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', marginTop: 10, fontSize: 11, color: 'var(--text-muted)' }}>
        <span>
          <span style={{ fontWeight: 600, color: 'var(--text-secondary)' }}>Volume:</span>{' '}
          <span className="font-mono">{position.volume} lots</span>
        </span>
        <span>
          <span style={{ fontWeight: 600, color: 'var(--text-secondary)' }}>Entry:</span>{' '}
          <span className="font-mono">{position.price_open.toFixed(digs)}</span>
        </span>
        <span>
          <span style={{ fontWeight: 600, color: 'var(--text-secondary)' }}>Now:</span>{' '}
          <span className="font-mono">{position.price_current.toFixed(digs)}</span>
        </span>
        {position.stop_loss > 0 && (
          <span>
            <span style={{ fontWeight: 600, color: 'var(--accent-red)' }}>SL:</span>{' '}
            <span className="font-mono" style={{ color: 'var(--accent-red)' }}>
              {slDollars != null ? `-$${slDollars.toFixed(2)}` : position.stop_loss.toFixed(digs)}
            </span>
            {slDollars != null && (
              <span className="text-muted" style={{ fontSize: 10 }}> ({position.stop_loss.toFixed(digs)})</span>
            )}
          </span>
        )}
        {position.take_profit > 0 && (
          <span>
            <span style={{ fontWeight: 600, color: 'var(--accent-green)' }}>TP:</span>{' '}
            <span className="font-mono" style={{ color: 'var(--accent-green)' }}>
              {tpDollars != null ? `+$${tpDollars.toFixed(2)}` : position.take_profit.toFixed(digs)}
            </span>
            {tpDollars != null && (
              <span className="text-muted" style={{ fontSize: 10 }}> ({position.take_profit.toFixed(digs)})</span>
            )}
          </span>
        )}
      </div>

      {/* Actions */}
      <div className="flex justify-between items-center mt-3">
        <div className="flex gap-3" style={{ fontSize: 11, color: 'var(--text-muted)' }}>
          {position.stop_loss > 0 && <span style={{ color: 'var(--accent-green)' }}>✓ Stop-loss {slDollars != null ? `(-$${slDollars.toFixed(0)})` : 'active'}</span>}
          {position.take_profit > 0 && <span style={{ color: 'var(--accent-green)' }}>✓ Take-profit {tpDollars != null ? `(+$${tpDollars.toFixed(0)})` : 'set'}</span>}
          {position.stop_loss === 0 && position.take_profit === 0 && <span>No SL/TP set</span>}
        </div>
        <button
          className={`btn ${position.profit >= 0 ? 'btn-success' : 'btn-warning'} btn-sm`}
          onClick={handleClose}
          disabled={closing}
          style={{ fontSize: 12 }}
        >
          {closing ? <span className="loading-spinner" /> : <DollarSign size={12} />}
          {position.profit >= 0 ? 'Sell & Take Profit' : 'Sell & Cut Loss'}
        </button>
      </div>
    </div>
  );
}

// Compact market row with Buy button for any symbol
function MarketRow({ rec, currency, onExecuted, isLast }: { rec: Recommendation; currency: string; onExecuted: () => void; isLast: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const [amount, setAmount] = useState('');
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<OrderResult | null>(null);
  const [volumeInfo, setVolumeInfo] = useState<{ volume: number; actual_cost: number; contract_size: number; price: number; min_sl_tp_dollars: number } | null>(null);
  // Manual action override: user can choose BUY or SELL regardless of AI signal
  const [manualAction, setManualAction] = useState<'BUY' | 'SELL'>(rec.signal.action === 'SELL' ? 'SELL' : 'BUY');

  // SL/TP as dollar amounts
  const [slDollar, setSlDollar] = useState('');
  const [tpDollar, setTpDollar] = useState('');

  const price = rec.entry_price_estimate || 0;
  const digits = price < 50 ? 5 : 2;
  const minSlTp = volumeInfo?.min_sl_tp_dollars || 0;

  // Fetch live tick data - only when visible (initial load + on expand)
  const [bid, setBid] = useState<number | null>(null);
  const [ask, setAsk] = useState<number | null>(null);
  const tickFetched = useRef(false);

  // Reset state when symbol changes (React may reuse component for different rec)
  const prevSymbol = useRef(rec.symbol);
  useEffect(() => {
    if (prevSymbol.current !== rec.symbol) {
      prevSymbol.current = rec.symbol;
      setExpanded(false);
      setSlDollar('');
      setTpDollar('');
      setAmount('');
      setResult(null);
      setVolumeInfo(null);
      setBid(null);
      setAsk(null);
      tickFetched.current = false;
    }
  }, [rec.symbol]);

  useEffect(() => {
    // Fetch once on mount
    if (!tickFetched.current) {
      tickFetched.current = true;
      api.getTick(rec.symbol).then((tick) => { setBid(tick.bid); setAsk(tick.ask); }).catch(() => {});
    }
  }, [rec.symbol]);

  // Refresh tick when expanded
  useEffect(() => {
    if (!expanded) return;
    let cancelled = false;
    const fetchTick = async () => {
      try {
        const tick = await api.getTick(rec.symbol);
        if (!cancelled) { setBid(tick.bid); setAsk(tick.ask); }
      } catch {}
    };
    fetchTick();
    const t = setInterval(fetchTick, 5000);
    return () => { cancelled = true; clearInterval(t); };
  }, [expanded, rec.symbol]);

  const name = getSymbolName(rec.symbol);
  const emoji = getSymbolEmoji(rec.symbol);
  const conf = Math.round(rec.signal.confidence * 100);
  const strength = getSignalStrength(rec.signal.confidence);

  // Convert dollar SL/TP to price levels for execution
  const isBuyAction = manualAction === 'BUY';
  const dollarToPrice = (dollarAmount: number, isSL: boolean) => {
    if (!volumeInfo || volumeInfo.volume <= 0 || volumeInfo.contract_size <= 0) return 0;
    const pricePerUnit = volumeInfo.volume * volumeInfo.contract_size;
    const livePrice = isBuyAction ? (ask || volumeInfo.price || rec.entry_price_estimate || 0) : (bid || volumeInfo.price || rec.entry_price_estimate || 0);
    if (livePrice <= 0) return 0;
    if (isBuyAction) {
      return isSL ? livePrice - dollarAmount / pricePerUnit : livePrice + dollarAmount / pricePerUnit;
    } else {
      // SELL: SL is above price, TP is below price
      return isSL ? livePrice + dollarAmount / pricePerUnit : livePrice - dollarAmount / pricePerUnit;
    }
  };

  const handleAmountChange = async (val: string) => {
    setAmount(val);
    setVolumeInfo(null);
    const num = parseFloat(val);
    if (num > 0) {
      try {
        const info = await api.calculateVolume(rec.symbol, num);
        setVolumeInfo(info);
        // Default SL/TP = 10% of investment, but at least the minimum required
        const defaultSlTp = Math.max(Math.round(num * 0.10), Math.ceil(info.min_sl_tp_dollars));
        setSlDollar(String(defaultSlTp));
        setTpDollar(String(defaultSlTp));
      } catch {}
    } else {
      setSlDollar('');
      setTpDollar('');
    }
  };

  const handleTrade = async () => {
    const num = parseFloat(amount);
    if (!num || num <= 0) return;
    // Convert dollar SL/TP to price levels
    const slAmt = parseFloat(slDollar) || 0;
    const tpAmt = parseFloat(tpDollar) || 0;
    const slNum = slAmt > 0 ? dollarToPrice(slAmt, true) : undefined;
    const tpNum = tpAmt > 0 ? dollarToPrice(tpAmt, false) : undefined;
    setLoading(true);
    try {
      const res = await api.quickBuy(rec.symbol, num, slNum, tpNum, manualAction);
      setResult(res);
      if (res.success) onExecuted();
    } catch (e: any) {
      setResult({ success: false, retcode: -1, retcode_desc: e.message, ticket: null, volume: null, price: null, stop_loss: null, take_profit: null, comment: '' });
    } finally {
      setLoading(false);
    }
  };

  const handleExpand = () => {
    if (!result) {
      setExpanded(!expanded);
    }
  };

  return (
    <div style={{ borderBottom: isLast ? 'none' : '1px solid var(--border)' }}>
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '10px 16px', cursor: 'pointer',
      }} onClick={handleExpand}>
        <div className="flex items-center gap-3" style={{ flex: 1, minWidth: 0 }}>
          <span style={{ fontSize: 16 }}>{emoji}</span>
          <div style={{ minWidth: 0 }}>
            <div style={{ fontWeight: 600, fontSize: 13 }}>{name}</div>
            <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>{rec.symbol}</div>
          </div>
        </div>

        {/* Live Bid / Ask */}
        <div style={{ minWidth: 130, textAlign: 'center', flexShrink: 0, marginRight: 8 }}>
          {bid !== null && ask !== null ? (
            <div style={{ display: 'flex', gap: 8, fontSize: 11, fontFamily: 'monospace' }}>
              <div>
                <span style={{ fontSize: 9, color: 'var(--accent-red)', fontWeight: 600, display: 'block' }}>SELL</span>
                <span style={{ color: 'var(--text-secondary)' }}>{bid.toFixed(digits)}</span>
              </div>
              <div>
                <span style={{ fontSize: 9, color: 'var(--accent-green)', fontWeight: 600, display: 'block' }}>BUY</span>
                <span style={{ color: 'var(--text-secondary)' }}>{ask.toFixed(digits)}</span>
              </div>
            </div>
          ) : (
            <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>Loading...</span>
          )}
        </div>

        <div className="flex items-center gap-2" style={{ width: 100, flexShrink: 0 }}>
          <div style={{ flex: 1, height: 5, background: 'var(--bg-primary)', borderRadius: 3, overflow: 'hidden' }}>
            <div style={{ width: `${conf}%`, height: '100%', background: strength.color, borderRadius: 3 }} />
          </div>
          <span style={{ fontSize: 10, color: strength.color, fontWeight: 600 }}>{conf}%</span>
        </div>

        {result ? (
          <span style={{ fontSize: 11, color: result.success ? 'var(--accent-green)' : 'var(--accent-red)', marginLeft: 12 }}>
            {result.success ? `${manualAction === 'SELL' ? 'Sold' : 'Bought'}! #${result.ticket}` : result.retcode_desc}
          </span>
        ) : (
          <button
            className={`btn ${manualAction === 'SELL' ? 'btn-danger' : 'btn-success'} btn-sm`}
            onClick={(e) => { e.stopPropagation(); handleExpand(); }}
            style={{ fontSize: 11, padding: '4px 12px', marginLeft: 12, flexShrink: 0 }}
          >
            <DollarSign size={10} /> Trade
          </button>
        )}
      </div>

      {expanded && !result && (
        <div style={{ padding: '0 16px 12px 16px' }}>
          {/* Buy/Sell Toggle */}
          <div className="flex items-center gap-1 mb-2">
            <button
              className={`btn btn-sm ${manualAction === 'BUY' ? 'btn-success' : 'btn-secondary'}`}
              onClick={(e) => { e.stopPropagation(); setManualAction('BUY'); }}
              style={{ fontSize: 11, padding: '3px 14px', fontWeight: 600 }}
            >
              ▲ Buy
            </button>
            <button
              className={`btn btn-sm ${manualAction === 'SELL' ? 'btn-danger' : 'btn-secondary'}`}
              onClick={(e) => { e.stopPropagation(); setManualAction('SELL'); }}
              style={{ fontSize: 11, padding: '3px 14px', fontWeight: 600 }}
            >
              ▼ Sell
            </button>
            {rec.signal.action !== 'HOLD' && manualAction !== rec.signal.action && (
              <span style={{ fontSize: 9, color: 'var(--accent-yellow)', marginLeft: 6 }}>⚠ AI recommends {rec.signal.action}</span>
            )}
          </div>
          {/* Amount row */}
          <div className="flex items-center gap-2" style={{ flexWrap: 'wrap' }}>
            {[10, 25, 50, 100].map((preset) => (
              <button
                key={preset}
                className={`btn ${amount === String(preset) ? (manualAction === 'SELL' ? 'btn-danger' : 'btn-success') : 'btn-secondary'} btn-sm`}
                onClick={() => handleAmountChange(String(preset))}
                style={{ fontSize: 11, padding: '4px 10px' }}
              >
                ${preset}
              </button>
            ))}
            <div style={{ position: 'relative', width: 100 }}>
              <span style={{ position: 'absolute', left: 8, top: '50%', transform: 'translateY(-50%)', color: 'var(--text-muted)', fontSize: 12 }}>$</span>
              <input
                className="form-input"
                type="number"
                placeholder="Amount"
                value={amount}
                onChange={(e) => handleAmountChange(e.target.value)}
                onClick={(e) => e.stopPropagation()}
                style={{ fontSize: 12, padding: '4px 8px 4px 20px', width: '100%' }}
                min="1"
              />
            </div>
            <button
              className={`btn ${manualAction === 'SELL' ? 'btn-danger' : 'btn-success'} btn-sm`}
              onClick={(e) => { e.stopPropagation(); handleTrade(); }}
              disabled={loading || !amount || parseFloat(amount) <= 0 || (parseFloat(slDollar) > 0 && parseFloat(slDollar) < minSlTp) || (parseFloat(tpDollar) > 0 && parseFloat(tpDollar) < minSlTp)}
              style={{ fontSize: 11, padding: '4px 14px', fontWeight: 600 }}
            >
              {loading ? <span className="loading-spinner" style={{ width: 10, height: 10 }} /> : <Play size={10} />}
              {amount ? `${manualAction === 'SELL' ? 'Sell' : 'Buy'} $${amount}` : (manualAction === 'SELL' ? 'Sell' : 'Buy')}
            </button>
          </div>

          {/* Volume info */}
          {volumeInfo && (
            <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 4 }}>
              Volume: <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{volumeInfo.volume} lots</span>
              {' '}(~${volumeInfo.actual_cost.toLocaleString()} market value)
            </div>
          )}

          {/* SL / TP row - dollar based */}
          {volumeInfo && (
            <div className="flex items-center gap-3 mt-2" style={{ flexWrap: 'wrap' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                <span style={{ fontSize: 10, color: 'var(--accent-red)', fontWeight: 600 }}>Max Loss $</span>
                <input className="form-input" type="number" value={slDollar}
                  onChange={(e) => setSlDollar(e.target.value)} onClick={(e) => e.stopPropagation()}
                  style={{ fontSize: 11, padding: '3px 6px', width: 70, fontFamily: 'monospace', borderColor: parseFloat(slDollar) > 0 && parseFloat(slDollar) < minSlTp ? '#ef4444' : undefined }} step="1" min={Math.ceil(minSlTp)} />
                {parseFloat(slDollar) > 0 && parseFloat(slDollar) < minSlTp ? (
                  <span style={{ fontSize: 9, color: '#ef4444', fontWeight: 600 }}>min ${Math.ceil(minSlTp)}</span>
                ) : slDollar && parseFloat(amount) > 0 ? (
                  <span style={{ fontSize: 9, color: 'var(--accent-red)' }}>
                    ${amount}→${(parseFloat(amount) - parseFloat(slDollar)).toFixed(0)}
                  </span>
                ) : null}
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                <span style={{ fontSize: 10, color: 'var(--accent-green)', fontWeight: 600 }}>Target +$</span>
                <input className="form-input" type="number" value={tpDollar}
                  onChange={(e) => setTpDollar(e.target.value)} onClick={(e) => e.stopPropagation()}
                  style={{ fontSize: 11, padding: '3px 6px', width: 70, fontFamily: 'monospace', borderColor: parseFloat(tpDollar) > 0 && parseFloat(tpDollar) < minSlTp ? '#ef4444' : undefined }} step="1" min={Math.ceil(minSlTp)} />
                {parseFloat(tpDollar) > 0 && parseFloat(tpDollar) < minSlTp ? (
                  <span style={{ fontSize: 9, color: '#ef4444', fontWeight: 600 }}>min ${Math.ceil(minSlTp)}</span>
                ) : tpDollar && parseFloat(amount) > 0 ? (
                  <span style={{ fontSize: 9, color: 'var(--accent-green)' }}>
                    ${amount}→${(parseFloat(amount) + parseFloat(tpDollar)).toFixed(0)}
                  </span>
                ) : null}
              </div>
            </div>
          )}
          {rec.explanation && (
            <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 6 }}>{rec.explanation}</div>
          )}
        </div>
      )}
    </div>
  );
}

export default function SimpleDashboard({ status, onRefresh }: Props) {
  const [recommendations, setRecommendations] = useState<Recommendation[]>([]);
  const [positions, setPositions] = useState<PositionInfo[]>([]);
  const [scanning, setScanning] = useState(false);
  const [lastScan, setLastScan] = useState<number | null>(null);
  const [error, setError] = useState('');
  const [selectedCategory, setSelectedCategory] = useState<string>('All');

  // Auto-trade state
  const [autoTradeRunning, setAutoTradeRunning] = useState(false);
  const [autoTradeLoading, setAutoTradeLoading] = useState(false);
  const [autoTradeLogs, setAutoTradeLogs] = useState<Array<{
    timestamp: number; symbol: string; action: string;
    confidence: number; detail: string; success: boolean;
  }>>([]);
  const [aiActivity, setAiActivity] = useState<Array<{
    timestamp: number; action: string; symbol: string;
    ticket: number; detail: string; profit: number; source?: string;
  }>>([]);
  const [posManagerRunning, setPosManagerRunning] = useState(false);

  const connected = status?.connected ?? false;
  const account = status?.account;
  const currency = account?.currency || 'USD';

  const refreshPositions = useCallback(async () => {
    if (!connected) return;
    try {
      const pos = await api.getPositions();
      setPositions(pos);
    } catch {}
  }, [connected]);

  const scanMarkets = useCallback(async () => {
    if (!connected) return;
    setScanning(true);
    setError('');
    try {
      const result = await api.smartEvaluate();
      setRecommendations(result.recommendations);
      setLastScan(result.scanned_at);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setScanning(false);
    }
  }, [connected]);

  useEffect(() => {
    refreshPositions();
    const t = setInterval(refreshPositions, 5000);
    return () => clearInterval(t);
  }, [refreshPositions]);

  useEffect(() => {
    if (connected) {
      scanMarkets();
      const t = setInterval(scanMarkets, 60000);
      return () => clearInterval(t);
    }
  }, [connected, scanMarkets]);

  // Poll auto-trade status
  const refreshAutoTrade = useCallback(async () => {
    if (!connected) return;
    try {
      const st = await api.getAutoTradeStatus();
      setAutoTradeRunning(st.running);
      setAutoTradeLogs(st.recent_trades);
      setPosManagerRunning(st.position_manager_running ?? false);
    } catch {}
  }, [connected]);

  useEffect(() => {
    refreshAutoTrade();
    const t = setInterval(refreshAutoTrade, 5000);
    return () => clearInterval(t);
  }, [refreshAutoTrade]);

  // Poll AI activity when auto-trade is running
  const refreshAIActivity = useCallback(async () => {
    if (!connected || !autoTradeRunning) return;
    try {
      const data = await api.getAIActivity(30);
      setAiActivity(data.live_activity || []);
      setPosManagerRunning(data.position_manager?.running ?? false);
    } catch {}
  }, [connected, autoTradeRunning]);

  useEffect(() => {
    if (autoTradeRunning) {
      refreshAIActivity();
      const t = setInterval(refreshAIActivity, 10000);
      return () => clearInterval(t);
    } else {
      setAiActivity([]);
    }
  }, [autoTradeRunning, refreshAIActivity]);

  const toggleAutoTrade = async () => {
    setAutoTradeLoading(true);
    try {
      if (autoTradeRunning) {
        await api.stopAutoTrade();
        setAutoTradeRunning(false);
      } else {
        await api.startAutoTrade();
        setAutoTradeRunning(true);
      }
    } catch (e: any) {
      setError(e.message);
    } finally {
      setAutoTradeLoading(false);
    }
  };

  const handleClosePosition = async (ticket: number) => {
    try {
      await api.closePosition(ticket);
      refreshPositions();
    } catch (e: any) {
      setError(e.message);
    }
  };

  const handlePanicStop = async () => {
    try {
      const isPanic = status?.panic_stop ?? false;
      await api.setPanicStop(!isPanic);
      onRefresh();
    } catch {}
  };

  // Portfolio SL/TP state
  const [portfolioSL, setPortfolioSL] = useState<number | null>(null);  // dollar amount to close at loss
  const [portfolioTP, setPortfolioTP] = useState<number | null>(null);  // dollar amount to close at profit
  const [editingPortfolioTargets, setEditingPortfolioTargets] = useState(false);
  const [tempSL, setTempSL] = useState('');
  const [tempTP, setTempTP] = useState('');

  // Auto-close when portfolio SL/TP is hit
  useEffect(() => {
    if (positions.length === 0) return;
    const total = positions.reduce((s, p) => s + p.profit, 0);
    if (portfolioSL !== null && total <= -Math.abs(portfolioSL)) {
      // Hit portfolio stop loss — close all
      setPortfolioSL(null);
      setPortfolioTP(null);
      (async () => {
        for (const p of positions) {
          try { await api.closePosition(p.ticket); } catch {}
        }
        refreshPositions();
        onRefresh();
      })();
    }
    if (portfolioTP !== null && total >= Math.abs(portfolioTP)) {
      // Hit portfolio take profit — close all
      setPortfolioSL(null);
      setPortfolioTP(null);
      (async () => {
        for (const p of positions) {
          try { await api.closePosition(p.ticket); } catch {}
        }
        refreshPositions();
        onRefresh();
      })();
    }
  }, [positions, portfolioSL, portfolioTP]);

  const [closingAll, setClosingAll] = useState(false);
  const handleSellAll = async () => {
    if (!confirm(`Close all ${positions.length} positions? This cannot be undone.`)) return;
    setClosingAll(true);
    try {
      let closed = 0;
      for (const p of positions) {
        try {
          await api.closePosition(p.ticket);
          closed++;
        } catch {}
      }
      refreshPositions();
      onRefresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setClosingAll(false);
    }
  };

  const totalProfit = positions.reduce((s, p) => s + p.profit, 0);

  // Category filter
  const filteredRecs = selectedCategory === 'All'
    ? recommendations
    : recommendations.filter((r) => (r.category || 'Other') === selectedCategory);
  const actionableRecs = filteredRecs.filter((r) => r.signal.action === 'BUY' || r.signal.action === 'SELL');
  const holdRecs = filteredRecs.filter((r) => r.signal.action === 'HOLD');

  // Count categories for tabs
  const categoryCounts: Record<string, number> = { All: recommendations.length };
  recommendations.forEach((r) => {
    const cat = r.category || 'Other';
    categoryCounts[cat] = (categoryCounts[cat] || 0) + 1;
  });
  const categoryOrder = ['All', 'Stocks', 'Crypto', 'Indices', 'Commodities', 'Forex', 'Other'];
  const availableCategories = categoryOrder.filter((c) => (categoryCounts[c] || 0) > 0);

  // Not connected state
  if (!connected) {
    return (
      <div>
        <ConnectionBar status={status} onRefresh={onRefresh} />
        <div style={{
          textAlign: 'center',
          padding: '80px 40px',
          maxWidth: 500,
          margin: '0 auto',
        }}>
          <div style={{ fontSize: 48, marginBottom: 16 }}>&#128202;</div>
          <h2 style={{ fontSize: 22, fontWeight: 600, marginBottom: 8 }}>Welcome to Your Trading Assistant</h2>
          <p style={{ color: 'var(--text-secondary)', fontSize: 14, lineHeight: 1.7, marginBottom: 24 }}>
            This app uses AI to analyze currency and commodity markets, then gives you
            simple buy/sell recommendations. You just click a button to trade.
          </p>
          <div style={{
            background: 'var(--bg-secondary)',
            borderRadius: 12,
            padding: '24px',
            textAlign: 'left',
            marginBottom: 24,
          }}>
            <h3 style={{ fontSize: 14, fontWeight: 600, marginBottom: 16 }}>How it works:</h3>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              {[
                { step: '1', text: 'Connect your MT5 demo account' },
                { step: '2', text: 'The AI scans markets automatically' },
                { step: '3', text: 'You see clear Buy/Sell recommendations' },
                { step: '4', text: 'Click "Buy Now" or "Sell Now" to trade' },
                { step: '5', text: 'The app protects you with automatic stop-loss' },
              ].map((item) => (
                <div key={item.step} className="flex items-center gap-3" style={{ fontSize: 13 }}>
                  <div style={{
                    width: 28, height: 28, borderRadius: '50%',
                    background: 'var(--accent-blue)', color: 'white',
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                    fontSize: 12, fontWeight: 700, flexShrink: 0,
                  }}>
                    {item.step}
                  </div>
                  <span>{item.text}</span>
                </div>
              ))}
            </div>
          </div>
          <a href="#/connection" className="btn btn-primary" style={{
            textDecoration: 'none',
            fontSize: 15,
            padding: '14px 32px',
          }}>
            Get Started - Connect Account
          </a>
        </div>
      </div>
    );
  }

  return (
    <div>
      <ConnectionBar status={status} onRefresh={onRefresh} />

      {/* Account Summary */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 12, marginTop: 16 }}>
        <div className="card" style={{ textAlign: 'center', padding: '14px 10px' }}>
          <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 4 }}>Balance</div>
          <div style={{ fontSize: 20, fontWeight: 700, fontFamily: 'monospace' }}>
            ${account?.balance.toFixed(2)}
          </div>
        </div>
        <div className="card" style={{ textAlign: 'center', padding: '14px 10px' }}>
          <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 4 }}>Equity (Live)</div>
          <div style={{ fontSize: 20, fontWeight: 700, fontFamily: 'monospace' }}>
            ${account?.equity.toFixed(2)}
          </div>
          {account && account.equity !== account.balance && (
            <div style={{
              fontSize: 11, fontWeight: 600, marginTop: 2,
              color: account.equity >= account.balance ? 'var(--accent-green)' : 'var(--accent-red)',
            }}>
              {account.equity >= account.balance ? '+' : ''}{(account.equity - account.balance).toFixed(2)}
            </div>
          )}
        </div>
        <div className="card" style={{ textAlign: 'center', padding: '14px 10px' }}>
          <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 4 }}>Margin Used</div>
          <div style={{ fontSize: 20, fontWeight: 700, fontFamily: 'monospace', color: (account?.margin || 0) > 0 ? 'var(--accent-yellow)' : 'var(--text-primary)' }}>
            ${account?.margin?.toFixed(2) || '0.00'}
          </div>
        </div>
        <div className="card" style={{ textAlign: 'center', padding: '14px 10px' }}>
          <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 4 }}>Free Margin</div>
          <div style={{ fontSize: 20, fontWeight: 700, fontFamily: 'monospace' }}>
            ${account?.free_margin?.toFixed(2) || '0.00'}
          </div>
          {account && account.leverage > 0 && (
            <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 2 }}>
              Leverage: 1:{account.leverage}
            </div>
          )}
        </div>
        <div className="card" style={{ textAlign: 'center', padding: '14px 10px' }}>
          <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 4 }}>Active Trades</div>
          <div style={{ fontSize: 20, fontWeight: 700 }}>{positions.length}</div>
          {positions.length > 0 && (
            <div style={{
              fontSize: 11, fontWeight: 600, marginTop: 2,
              color: totalProfit >= 0 ? 'var(--accent-green)' : 'var(--accent-red)',
            }}>
              {totalProfit >= 0 ? '+' : ''}{totalProfit.toFixed(2)} {currency}
            </div>
          )}
        </div>
      </div>
      {/* Balance explanation */}
      {positions.length > 0 && account && account.equity !== account.balance && (
        <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 6, textAlign: 'center' }}>
          Balance stays the same while trades are open. Equity = Balance + open trade profits/losses. Free Margin = what you can still use.
        </div>
      )}

      {/* Panic Stop */}
      {status?.panic_stop && (
        <div style={{
          background: 'rgba(239, 68, 68, 0.1)',
          border: '1px solid rgba(239, 68, 68, 0.3)',
          borderRadius: 8,
          padding: '16px 20px',
          marginTop: 16,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
        }}>
          <div className="flex items-center gap-3">
            <AlertOctagon size={20} style={{ color: 'var(--accent-red)' }} />
            <div>
              <div style={{ fontWeight: 600, color: 'var(--accent-red)', fontSize: 14 }}>Trading Paused</div>
              <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>All trading is stopped. Click Resume when ready.</div>
            </div>
          </div>
          <button className="btn btn-success" onClick={handlePanicStop} style={{ fontSize: 14, padding: '10px 24px' }}>
            Resume Trading
          </button>
        </div>
      )}

      {/* Auto-Trade Panel */}
      <div className="card mt-4" style={{
        borderLeft: `4px solid ${autoTradeRunning ? 'var(--accent-green)' : 'var(--border)'}`,
        padding: '16px 20px',
      }}>
        <div className="flex justify-between items-center">
          <div className="flex items-center gap-3">
            <Zap size={20} style={{ color: autoTradeRunning ? 'var(--accent-green)' : 'var(--text-muted)' }} />
            <div>
              <div style={{ fontWeight: 600, fontSize: 15 }}>
                Auto-Trading {autoTradeRunning ? 'Active' : 'Off'}
              </div>
              <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                {autoTradeRunning
                  ? `AI is scanning, trading & managing positions autonomously${posManagerRunning ? ' • Position Manager active' : ''}`
                  : 'Turn on to let the AI trade and manage positions for you'
                }
              </div>
            </div>
          </div>
          <button
            className={`btn ${autoTradeRunning ? 'btn-danger' : 'btn-success'}`}
            onClick={toggleAutoTrade}
            disabled={autoTradeLoading || !!status?.panic_stop}
            style={{ fontSize: 14, padding: '10px 24px', fontWeight: 600 }}
          >
            {autoTradeLoading ? (
              <span className="loading-spinner" />
            ) : autoTradeRunning ? (
              <><Power size={14} /> Turn Off</>
            ) : (
              <><Zap size={14} /> Turn On</>
            )}
          </button>
        </div>

        {/* Auto-trade activity log */}
        {autoTradeRunning && autoTradeLogs.length > 0 && (
          <div style={{ marginTop: 12, borderTop: '1px solid var(--border)', paddingTop: 12 }}>
            <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 8, color: 'var(--text-muted)' }}>
              Recent Auto-Trades
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4, maxHeight: 150, overflowY: 'auto' }}>
              {autoTradeLogs.slice().reverse().slice(0, 10).map((log, i) => (
                <div key={i} className="flex items-center gap-2" style={{ fontSize: 12, padding: '4px 0' }}>
                  {log.success ? (
                    <CheckCircle size={12} style={{ color: 'var(--accent-green)', flexShrink: 0 }} />
                  ) : (
                    <XCircle size={12} style={{ color: 'var(--accent-red)', flexShrink: 0 }} />
                  )}
                  <span style={{ fontWeight: 600 }}>
                    {getSymbolEmoji(log.symbol)} {log.action} {getSymbolName(log.symbol)}
                  </span>
                  <span className="text-muted">
                    ({Math.round(log.confidence * 100)}%)
                  </span>
                  <span style={{ color: log.success ? 'var(--accent-green)' : 'var(--accent-red)' }}>
                    {log.detail.length > 40 ? log.detail.substring(0, 40) + '...' : log.detail}
                  </span>
                  <span className="text-muted" style={{ marginLeft: 'auto', fontSize: 11 }}>
                    {new Date(log.timestamp * 1000).toLocaleTimeString()}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}

        {autoTradeRunning && autoTradeLogs.length === 0 && (
          <div style={{ marginTop: 12, fontSize: 12, color: 'var(--text-muted)', textAlign: 'center', padding: 8 }}>
            <span className="loading-spinner" style={{ width: 12, height: 12, marginRight: 6 }} />
            Waiting for next market scan...
          </div>
        )}
      </div>

      {/* Active Trades */}
      {positions.length > 0 && (
        <div className="mt-4">
          {/* Portfolio Summary Card */}
          {(() => {
            const totalInvested = positions.reduce((s, p) => s + Math.abs(p.volume * p.price_open * 100), 0); // approximate
            const profitPct = account?.balance ? (totalProfit / account.balance * 100) : 0;
            const profitColor = totalProfit >= 0 ? 'var(--accent-green)' : 'var(--accent-red)';
            const winning = positions.filter(p => p.profit > 0).length;
            const losing = positions.filter(p => p.profit < 0).length;

            return (
              <div className="card" style={{ padding: '16px 20px', marginBottom: 12, borderLeft: `3px solid ${profitColor}` }}>
                {/* Row 1: Title + Sell All button */}
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
                  <h3 style={{ fontSize: 16, fontWeight: 600, margin: 0 }}>
                    Your Active Trades
                    <span style={{ fontSize: 12, fontWeight: 400, color: 'var(--text-muted)', marginLeft: 8 }}>
                      ({positions.length})
                    </span>
                  </h3>
                  <button
                    className="btn"
                    onClick={handleSellAll}
                    disabled={closingAll}
                    style={{
                      fontSize: 12, padding: '6px 16px',
                      background: 'var(--accent-red)', color: '#fff',
                      border: 'none', borderRadius: 6, cursor: 'pointer',
                      opacity: closingAll ? 0.6 : 1,
                    }}
                  >
                    {closingAll ? 'Closing...' : `Sell All (${positions.length})`}
                  </button>
                </div>

                {/* Row 2: Combined P/L summary */}
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr 1fr', gap: 12, marginBottom: 12 }}>
                  <div style={{ textAlign: 'center' }}>
                    <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 2 }}>Combined P/L</div>
                    <div style={{ fontSize: 18, fontWeight: 700, fontFamily: 'monospace', color: profitColor }}>
                      {totalProfit >= 0 ? '+' : ''}{totalProfit.toFixed(2)}
                    </div>
                    <div style={{ fontSize: 11, color: profitColor, fontFamily: 'monospace' }}>
                      {profitPct >= 0 ? '+' : ''}{profitPct.toFixed(2)}%
                    </div>
                  </div>
                  <div style={{ textAlign: 'center' }}>
                    <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 2 }}>Winning</div>
                    <div style={{ fontSize: 18, fontWeight: 700, fontFamily: 'monospace', color: 'var(--accent-green)' }}>{winning}</div>
                    <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>trades</div>
                  </div>
                  <div style={{ textAlign: 'center' }}>
                    <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 2 }}>Losing</div>
                    <div style={{ fontSize: 18, fontWeight: 700, fontFamily: 'monospace', color: 'var(--accent-red)' }}>{losing}</div>
                    <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>trades</div>
                  </div>
                  <div style={{ textAlign: 'center' }}>
                    <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 2 }}>Win Rate</div>
                    <div style={{ fontSize: 18, fontWeight: 700, fontFamily: 'monospace' }}>
                      {positions.length > 0 ? Math.round(winning / positions.length * 100) : 0}%
                    </div>
                    <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>ratio</div>
                  </div>
                </div>

                {/* P/L Progress Bar */}
                {(portfolioSL !== null || portfolioTP !== null) && (
                  <div style={{ marginBottom: 12 }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 10, color: 'var(--text-muted)', marginBottom: 4 }}>
                      <span style={{ color: 'var(--accent-red)' }}>SL: -${Math.abs(portfolioSL || 0)}</span>
                      <span style={{ fontWeight: 600 }}>${totalProfit.toFixed(2)}</span>
                      <span style={{ color: 'var(--accent-green)' }}>TP: +${Math.abs(portfolioTP || 0)}</span>
                    </div>
                    <div style={{ height: 8, background: 'var(--bg-tertiary)', borderRadius: 4, overflow: 'hidden', position: 'relative' }}>
                      {(() => {
                        const sl = Math.abs(portfolioSL || 500);
                        const tp = Math.abs(portfolioTP || 500);
                        const range = sl + tp;
                        const pos = ((totalProfit + sl) / range) * 100;
                        const clamped = Math.max(0, Math.min(100, pos));
                        return (
                          <>
                            {/* SL zone (red, left half) */}
                            <div style={{ position: 'absolute', left: 0, top: 0, bottom: 0, width: `${(sl / range) * 100}%`, background: 'rgba(239,68,68,0.15)' }} />
                            {/* TP zone (green, right half) */}
                            <div style={{ position: 'absolute', right: 0, top: 0, bottom: 0, width: `${(tp / range) * 100}%`, background: 'rgba(34,197,94,0.15)' }} />
                            {/* Current position marker */}
                            <div style={{
                              position: 'absolute', left: `${clamped}%`, top: -2, width: 4, height: 12, borderRadius: 2,
                              background: totalProfit >= 0 ? 'var(--accent-green)' : 'var(--accent-red)',
                              transform: 'translateX(-50%)',
                              transition: 'left 0.5s ease',
                            }} />
                            {/* Center line (breakeven) */}
                            <div style={{ position: 'absolute', left: `${(sl / range) * 100}%`, top: 0, bottom: 0, width: 1, background: 'var(--text-muted)', opacity: 0.4 }} />
                          </>
                        );
                      })()}
                    </div>
                  </div>
                )}

                {/* Portfolio SL/TP Controls */}
                {editingPortfolioTargets ? (
                  <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                      <span style={{ fontSize: 11, color: 'var(--accent-red)', fontWeight: 600 }}>SL $</span>
                      <input
                        type="number" value={tempSL} onChange={(e) => setTempSL(e.target.value)}
                        placeholder="e.g. 500"
                        style={{ width: 80, padding: '4px 8px', fontSize: 12, borderRadius: 4, border: '1px solid var(--border)', background: 'var(--bg-tertiary)', color: 'var(--text-primary)' }}
                      />
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                      <span style={{ fontSize: 11, color: 'var(--accent-green)', fontWeight: 600 }}>TP $</span>
                      <input
                        type="number" value={tempTP} onChange={(e) => setTempTP(e.target.value)}
                        placeholder="e.g. 1000"
                        style={{ width: 80, padding: '4px 8px', fontSize: 12, borderRadius: 4, border: '1px solid var(--border)', background: 'var(--bg-tertiary)', color: 'var(--text-primary)' }}
                      />
                    </div>
                    <button
                      onClick={() => {
                        setPortfolioSL(tempSL ? parseFloat(tempSL) : null);
                        setPortfolioTP(tempTP ? parseFloat(tempTP) : null);
                        setEditingPortfolioTargets(false);
                      }}
                      style={{ fontSize: 11, padding: '4px 12px', background: 'var(--accent-blue)', color: '#fff', border: 'none', borderRadius: 4, cursor: 'pointer' }}
                    >
                      Set
                    </button>
                    <button
                      onClick={() => { setEditingPortfolioTargets(false); }}
                      style={{ fontSize: 11, padding: '4px 8px', background: 'transparent', color: 'var(--text-muted)', border: '1px solid var(--border)', borderRadius: 4, cursor: 'pointer' }}
                    >
                      Cancel
                    </button>
                  </div>
                ) : (
                  <div style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 11 }}>
                    {portfolioSL !== null && (
                      <span style={{ color: 'var(--accent-red)', background: 'rgba(239,68,68,0.1)', padding: '2px 8px', borderRadius: 4 }}>
                        Stop Loss: -${Math.abs(portfolioSL)} {totalProfit <= -Math.abs(portfolioSL) * 0.8 ? '⚠️' : ''}
                      </span>
                    )}
                    {portfolioTP !== null && (
                      <span style={{ color: 'var(--accent-green)', background: 'rgba(34,197,94,0.1)', padding: '2px 8px', borderRadius: 4 }}>
                        Take Profit: +${Math.abs(portfolioTP)} {totalProfit >= Math.abs(portfolioTP) * 0.8 ? '🎯' : ''}
                      </span>
                    )}
                    <button
                      onClick={() => {
                        setTempSL(portfolioSL ? String(Math.abs(portfolioSL)) : '');
                        setTempTP(portfolioTP ? String(Math.abs(portfolioTP)) : '');
                        setEditingPortfolioTargets(true);
                      }}
                      style={{ fontSize: 11, padding: '2px 10px', background: 'transparent', color: 'var(--accent-blue)', border: '1px solid var(--accent-blue)', borderRadius: 4, cursor: 'pointer' }}
                    >
                      {portfolioSL !== null || portfolioTP !== null ? '✏️ Edit Targets' : '🎯 Set Portfolio SL/TP'}
                    </button>
                    {(portfolioSL !== null || portfolioTP !== null) && (
                      <button
                        onClick={() => { setPortfolioSL(null); setPortfolioTP(null); }}
                        style={{ fontSize: 11, padding: '2px 8px', background: 'transparent', color: 'var(--text-muted)', border: '1px solid var(--border)', borderRadius: 4, cursor: 'pointer' }}
                      >
                        Clear
                      </button>
                    )}
                  </div>
                )}
              </div>
            );
          })()}

          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {positions.map((p) => (
              <PositionCard
                key={p.ticket}
                position={p}
                currency={currency}
                onClose={handleClosePosition}
                leverage={account?.leverage || 100}
              />
            ))}
          </div>
        </div>
      )}

      {/* AI Brain Activity — shows when auto-trade is on */}
      {autoTradeRunning && aiActivity.length > 0 && (
        <div className="mt-4">
          <h3 style={{ fontSize: 16, fontWeight: 600, marginBottom: 12, display: 'flex', alignItems: 'center', gap: 8 }}>
            <Zap size={16} style={{ color: 'var(--accent-blue)' }} />
            AI Brain Activity
            {posManagerRunning && (
              <span className="badge badge-blue" style={{ fontSize: 10, fontWeight: 500 }}>
                Position Manager Active
              </span>
            )}
          </h3>
          <div className="card" style={{ padding: 0, maxHeight: 260, overflowY: 'auto' }}>
            {aiActivity.map((activity, i) => {
              const actionIcons: Record<string, string> = {
                trailing_stop: '📈', breakeven: '🛡️', close_reversal: '🔄',
                close_timeout: '⏰', average_down: '📉', scanner: '🔍',
              };
              const actionColors: Record<string, string> = {
                trailing_stop: 'var(--accent-green)', breakeven: 'var(--accent-blue)',
                close_reversal: 'var(--accent-red)', close_timeout: 'var(--accent-red)',
                average_down: 'var(--accent-yellow, #f59e0b)', scanner: 'var(--accent-blue)',
              };
              const icon = actionIcons[activity.action] || (activity.source === 'scanner' ? '🔍' : '🤖');
              const color = actionColors[activity.action] || 'var(--text-secondary)';
              const timeStr = new Date(activity.timestamp * 1000).toLocaleTimeString();

              return (
                <div
                  key={`${activity.timestamp}-${activity.ticket ?? 0}-${i}`}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 10,
                    padding: '10px 14px', fontSize: 12,
                    borderBottom: i < aiActivity.length - 1 ? '1px solid var(--border)' : 'none',
                  }}
                >
                  <span style={{ fontSize: 16, flexShrink: 0 }}>{icon}</span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                      <span style={{ fontWeight: 600, color }}>{getSymbolEmoji(activity.symbol)} {getSymbolName(activity.symbol)}</span>
                      <span className="badge" style={{ fontSize: 9, background: color, color: '#fff', padding: '1px 6px' }}>
                        {activity.action.replace('_', ' ')}
                      </span>
                    </div>
                    <div className="text-muted" style={{ fontSize: 11, marginTop: 2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {activity.detail ?? ''}
                    </div>
                  </div>
                  <div style={{ textAlign: 'right', flexShrink: 0 }}>
                    {activity.profit != null && activity.profit !== 0 && (
                      <div style={{ fontFamily: 'monospace', fontWeight: 600, fontSize: 11, color: activity.profit >= 0 ? 'var(--accent-green)' : 'var(--accent-red)' }}>
                        {activity.profit >= 0 ? '+' : ''}{activity.profit.toFixed(2)}
                      </div>
                    )}
                    <div className="text-muted" style={{ fontSize: 10 }}>{timeStr}</div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Available Markets */}
      <div className="mt-4">
        <div className="flex justify-between items-center mb-3">
          <div>
            <h3 style={{ fontSize: 16, fontWeight: 600 }}>Available Markets</h3>
            <p style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 2 }}>
              {lastScan
                ? `AI analyzed ${recommendations.length} symbols \u2022 Last checked: ${new Date(lastScan * 1000).toLocaleTimeString()}`
                : 'Click "Scan All" to analyze every available market'
              }
            </p>
          </div>
          <div className="flex gap-2">
            <button className="btn btn-primary" onClick={scanMarkets} disabled={scanning} style={{ fontSize: 13, padding: '10px 20px' }}>
              {scanning ? <span className="loading-spinner" /> : <RefreshCw size={14} />}
              {scanning ? 'Analyzing...' : 'Scan All'}
            </button>
            {!status?.panic_stop ? (
              <button className="btn btn-secondary" onClick={handlePanicStop} style={{ fontSize: 12, color: 'var(--accent-red)' }}>
                <AlertOctagon size={12} />
                Stop All
              </button>
            ) : null}
          </div>
        </div>

        {error && (
          <div className="error-banner" style={{ fontSize: 13 }}>
            Something went wrong: {error}
          </div>
        )}

        {/* Category filter tabs */}
        {recommendations.length > 0 && (
          <div className="flex gap-2 mb-3" style={{ flexWrap: 'wrap' }}>
            {availableCategories.map((cat) => (
              <button
                key={cat}
                className={`btn ${selectedCategory === cat ? 'btn-primary' : 'btn-secondary'} btn-sm`}
                onClick={() => setSelectedCategory(cat)}
                style={{ fontSize: 12, padding: '6px 14px', borderRadius: 20 }}
              >
                {cat === 'All' ? '\uD83C\uDF10' : cat === 'Stocks' ? '\uD83D\uDCC8' : cat === 'Crypto' ? '\uD83D\uDCB0' : cat === 'Indices' ? '\uD83D\uDCCA' : cat === 'Commodities' ? '\uD83C\uDF1F' : cat === 'Forex' ? '\uD83D\uDCB1' : '\uD83D\uDCCB'}{' '}
                {cat} ({categoryCounts[cat]})
              </button>
            ))}
          </div>
        )}

        {scanning && recommendations.length === 0 && (
          <div style={{ textAlign: 'center', padding: '40px 20px' }}>
            <span className="loading-spinner" style={{ width: 32, height: 32, borderWidth: 3 }} />
            <p style={{ marginTop: 16, color: 'var(--text-muted)', fontSize: 14 }}>
              Scanning all available markets... This may take a moment.
            </p>
          </div>
        )}

        {!scanning && recommendations.length === 0 && (
          <div style={{
            textAlign: 'center',
            padding: '40px 20px',
            background: 'var(--bg-secondary)',
            borderRadius: 8,
            border: '1px solid var(--border)',
          }}>
            <div style={{ fontSize: 32, marginBottom: 8 }}>&#128269;</div>
            <p style={{ fontSize: 14, color: 'var(--text-muted)' }}>
              Click "Scan All" to see every available market with AI safety ratings
            </p>
          </div>
        )}

        {/* Show ALL recommendations - buy opportunities first, then waiting */}
        {recommendations.length > 0 && (
          <div>
            {/* Trade opportunities (BUY + SELL) */}
            {actionableRecs.length > 0 && (
              <div style={{ marginBottom: 16 }}>
                <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--accent-blue)', marginBottom: 8, display: 'flex', alignItems: 'center', gap: 6 }}>
                  <TrendingUp size={14} />
                  Trade Opportunities ({actionableRecs.length})
                  <span style={{ fontSize: 10, fontWeight: 400, color: 'var(--text-muted)' }}>
                    {actionableRecs.filter(r => r.signal.action === 'BUY').length} BUY
                    {' / '}
                    {actionableRecs.filter(r => r.signal.action === 'SELL').length} SELL
                  </span>
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                  {actionableRecs.map((rec, i) => (
                    <TradeCard key={`${rec.symbol}-${i}`} rec={rec} onExecuted={refreshPositions} currency={currency} />
                  ))}
                </div>
              </div>
            )}

            {/* All other symbols - with Buy button */}
            {holdRecs.length > 0 && (
              <div>
                <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-muted)', marginBottom: 8, display: 'flex', alignItems: 'center', gap: 6 }}>
                  <Clock size={14} />
                  All Markets ({holdRecs.length})
                </div>
                <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
                  {holdRecs.map((rec, i) => (
                    <MarketRow key={`hold-${i}`} rec={rec} currency={currency} onExecuted={refreshPositions} isLast={i === holdRecs.length - 1} />
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Beginner tip */}
      <div style={{
        marginTop: 24,
        padding: '14px 18px',
        background: 'rgba(59, 130, 246, 0.06)',
        border: '1px solid rgba(59, 130, 246, 0.15)',
        borderRadius: 8,
        fontSize: 12,
        color: 'var(--text-secondary)',
        display: 'flex',
        alignItems: 'center',
        gap: 10,
      }}>
        <HelpCircle size={16} style={{ color: 'var(--accent-blue)', flexShrink: 0 }} />
        <span>
          <strong>Tip:</strong> Green "Buy" cards are opportunities the AI recommends. The bar shows how confident the AI is.
          Items in "Waiting" are being watched but aren't ready yet. Every trade includes automatic stop-loss protection.
        </span>
      </div>
    </div>
  );
}
