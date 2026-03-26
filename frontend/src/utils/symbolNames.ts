// Human-readable names for trading symbols
const SYMBOL_NAMES: Record<string, { name: string; emoji: string; description: string }> = {
  // Crypto
  'BTCUSD': { name: 'Bitcoin', emoji: '\u{1F4B0}', description: 'Bitcoin price in US Dollars' },
  'BTCUSD.': { name: 'Bitcoin', emoji: '\u{1F4B0}', description: 'Bitcoin price in US Dollars' },
  'ETHUSD': { name: 'Ethereum', emoji: '\u{1F48E}', description: 'Ethereum price in US Dollars' },
  'ETHUSD.': { name: 'Ethereum', emoji: '\u{1F48E}', description: 'Ethereum price in US Dollars' },
  'LTCUSD': { name: 'Litecoin', emoji: '\u{1FA99}', description: 'Litecoin price in US Dollars' },
  'XRPUSD': { name: 'Ripple (XRP)', emoji: '\u{1FA99}', description: 'XRP price in US Dollars' },
  'SOLUSD': { name: 'Solana', emoji: '\u{2600}', description: 'Solana price in US Dollars' },
  'ADAUSD': { name: 'Cardano', emoji: '\u{1FA99}', description: 'Cardano (ADA) price in US Dollars' },
  'DOGUSD': { name: 'Dogecoin', emoji: '\u{1F436}', description: 'Dogecoin price in US Dollars' },
  'DOGEUSD': { name: 'Dogecoin', emoji: '\u{1F436}', description: 'Dogecoin price in US Dollars' },
  'BNBUSD': { name: 'BNB (Binance)', emoji: '\u{1FA99}', description: 'BNB price in US Dollars' },
  'AVAXUSD': { name: 'Avalanche', emoji: '\u{2744}', description: 'Avalanche (AVAX) price in US Dollars' },
  'DOTUSD': { name: 'Polkadot', emoji: '\u{1FA99}', description: 'Polkadot (DOT) price in US Dollars' },
  'LINKUSD': { name: 'Chainlink', emoji: '\u{1F517}', description: 'Chainlink price in US Dollars' },
  'UNIUSD': { name: 'Uniswap', emoji: '\u{1F984}', description: 'Uniswap price in US Dollars' },
  'MATICUSD': { name: 'Polygon', emoji: '\u{1FA99}', description: 'Polygon (MATIC) price in US Dollars' },

  // Stocks
  'AAPL': { name: 'Apple', emoji: '\u{1F34E}', description: 'Apple Inc. stock' },
  'AAPL.US': { name: 'Apple', emoji: '\u{1F34E}', description: 'Apple Inc. stock' },
  'MSFT': { name: 'Microsoft', emoji: '\u{1F4BB}', description: 'Microsoft Corp. stock' },
  'MSFT.US': { name: 'Microsoft', emoji: '\u{1F4BB}', description: 'Microsoft Corp. stock' },
  'GOOGL': { name: 'Google', emoji: '\u{1F50D}', description: 'Alphabet (Google) stock' },
  'GOOGL.US': { name: 'Google', emoji: '\u{1F50D}', description: 'Alphabet (Google) stock' },
  'AMZN': { name: 'Amazon', emoji: '\u{1F4E6}', description: 'Amazon.com stock' },
  'AMZN.US': { name: 'Amazon', emoji: '\u{1F4E6}', description: 'Amazon.com stock' },
  'TSLA': { name: 'Tesla', emoji: '\u{1F697}', description: 'Tesla Inc. stock' },
  'TSLA.US': { name: 'Tesla', emoji: '\u{1F697}', description: 'Tesla Inc. stock' },
  'META': { name: 'Meta (Facebook)', emoji: '\u{1F310}', description: 'Meta Platforms stock' },
  'META.US': { name: 'Meta (Facebook)', emoji: '\u{1F310}', description: 'Meta Platforms stock' },
  'NVDA': { name: 'NVIDIA', emoji: '\u{1F3AE}', description: 'NVIDIA Corp. stock' },
  'NVDA.US': { name: 'NVIDIA', emoji: '\u{1F3AE}', description: 'NVIDIA Corp. stock' },
  'NFLX': { name: 'Netflix', emoji: '\u{1F3AC}', description: 'Netflix Inc. stock' },
  'NFLX.US': { name: 'Netflix', emoji: '\u{1F3AC}', description: 'Netflix Inc. stock' },
  'AMD': { name: 'AMD', emoji: '\u{1F4BB}', description: 'Advanced Micro Devices stock' },
  'AMD.US': { name: 'AMD', emoji: '\u{1F4BB}', description: 'Advanced Micro Devices stock' },
  'DIS': { name: 'Disney', emoji: '\u{1F3F0}', description: 'Walt Disney Co. stock' },
  'DIS.US': { name: 'Disney', emoji: '\u{1F3F0}', description: 'Walt Disney Co. stock' },
  'BA': { name: 'Boeing', emoji: '\u{2708}', description: 'Boeing Co. stock' },
  'BA.US': { name: 'Boeing', emoji: '\u{2708}', description: 'Boeing Co. stock' },
  'V': { name: 'Visa', emoji: '\u{1F4B3}', description: 'Visa Inc. stock' },
  'V.US': { name: 'Visa', emoji: '\u{1F4B3}', description: 'Visa Inc. stock' },
  'JPM': { name: 'JPMorgan Chase', emoji: '\u{1F3E6}', description: 'JPMorgan Chase & Co. stock' },
  'JPM.US': { name: 'JPMorgan Chase', emoji: '\u{1F3E6}', description: 'JPMorgan Chase & Co. stock' },
  'WMT': { name: 'Walmart', emoji: '\u{1F6D2}', description: 'Walmart Inc. stock' },
  'WMT.US': { name: 'Walmart', emoji: '\u{1F6D2}', description: 'Walmart Inc. stock' },
  'KO': { name: 'Coca-Cola', emoji: '\u{1F964}', description: 'Coca-Cola Co. stock' },
  'KO.US': { name: 'Coca-Cola', emoji: '\u{1F964}', description: 'Coca-Cola Co. stock' },
  'NKE': { name: 'Nike', emoji: '\u{1F45F}', description: 'Nike Inc. stock' },
  'NKE.US': { name: 'Nike', emoji: '\u{1F45F}', description: 'Nike Inc. stock' },
  'INTC': { name: 'Intel', emoji: '\u{1F4BB}', description: 'Intel Corp. stock' },
  'INTC.US': { name: 'Intel', emoji: '\u{1F4BB}', description: 'Intel Corp. stock' },
  'PFE': { name: 'Pfizer', emoji: '\u{1F48A}', description: 'Pfizer Inc. stock' },
  'PFE.US': { name: 'Pfizer', emoji: '\u{1F48A}', description: 'Pfizer Inc. stock' },

  // Indices
  'US30': { name: 'Dow Jones', emoji: '\u{1F4C8}', description: 'US stock market index (30 companies)' },
  'US500': { name: 'S&P 500', emoji: '\u{1F4C8}', description: 'US stock market index (500 companies)' },
  'NAS100': { name: 'Nasdaq 100', emoji: '\u{1F4C8}', description: 'US tech stock index' },
  'US100': { name: 'Nasdaq 100', emoji: '\u{1F4C8}', description: 'US tech stock index' },
  'GER40': { name: 'DAX 40', emoji: '\u{1F1E9}\u{1F1EA}', description: 'German stock index' },
  'UK100': { name: 'FTSE 100', emoji: '\u{1F1EC}\u{1F1E7}', description: 'UK stock index' },
  'JPN225': { name: 'Nikkei 225', emoji: '\u{1F1EF}\u{1F1F5}', description: 'Japanese stock index' },

  // Commodities
  'XAUUSD': { name: 'Gold', emoji: '\u{1F947}', description: 'Gold price in US Dollars' },
  'XAGUSD': { name: 'Silver', emoji: '\u{1FA99}', description: 'Silver price in US Dollars' },
  'XPTUSD': { name: 'Platinum', emoji: '\u{1FA99}', description: 'Platinum price in US Dollars' },
  'USOIL': { name: 'US Oil (WTI)', emoji: '\u{1F6E2}', description: 'Crude oil price' },
  'UKOIL': { name: 'UK Oil (Brent)', emoji: '\u{1F6E2}', description: 'Brent crude oil price' },

  // Forex (kept for reference but de-prioritized)
  'EURUSD': { name: 'Euro / Dollar', emoji: '\u{1F1EA}\u{1F1FA}', description: 'Euro vs US Dollar' },
  'GBPUSD': { name: 'Pound / Dollar', emoji: '\u{1F1EC}\u{1F1E7}', description: 'British Pound vs US Dollar' },
  'USDJPY': { name: 'Dollar / Yen', emoji: '\u{1F1EF}\u{1F1F5}', description: 'US Dollar vs Japanese Yen' },
};

// Dynamic description cache (populated from MT5 data)
const dynamicNames: Record<string, { name: string; emoji: string; description: string }> = {};

export function registerSymbolFromMT5(symbol: string, description: string, category: string) {
  if (SYMBOL_NAMES[symbol]) return; // Already known
  const emoji = category === 'Crypto' ? '\u{1FA99}'
    : category === 'Stocks' ? '\u{1F4C8}'
    : category === 'Indices' ? '\u{1F4CA}'
    : category === 'Commodities' ? '\u{1F3ED}'
    : '\u{1F4B1}';
  dynamicNames[symbol] = { name: description || symbol, emoji, description: description || symbol };
}

export function getSymbolName(symbol: string): string {
  return SYMBOL_NAMES[symbol]?.name || dynamicNames[symbol]?.name || symbol;
}

export function getSymbolEmoji(symbol: string): string {
  return SYMBOL_NAMES[symbol]?.emoji || dynamicNames[symbol]?.emoji || '\u{1F4B1}';
}

export function getSymbolDescription(symbol: string): string {
  return SYMBOL_NAMES[symbol]?.description || dynamicNames[symbol]?.description || symbol;
}

// Signal strength labels
export function getSignalStrength(confidence: number): { label: string; color: string; description: string } {
  if (confidence >= 0.80) return { label: 'Very Strong', color: 'var(--accent-green)', description: 'The AI is highly confident in this trade' };
  if (confidence >= 0.65) return { label: 'Strong', color: 'var(--accent-green)', description: 'Good opportunity detected' };
  if (confidence >= 0.55) return { label: 'Moderate', color: 'var(--accent-yellow)', description: 'Decent opportunity, but not the strongest' };
  if (confidence >= 0.40) return { label: 'Weak', color: 'var(--accent-orange)', description: 'Low confidence - better to wait' };
  return { label: 'Very Weak', color: 'var(--text-muted)', description: 'Not recommended right now' };
}

export function getActionDescription(action: string, symbol: string): string {
  const name = getSymbolName(symbol);
  if (action === 'BUY') return `Buy ${name} (price expected to go UP)`;
  if (action === 'SELL') return `Sell ${name} (price expected to go DOWN)`;
  return `Wait on ${name} (no clear opportunity right now)`;
}

export function explainProfitLoss(amount: number, currency: string): string {
  if (amount === 0) return 'Breaking even';
  if (amount > 0) return `Making ${currency} ${amount.toFixed(2)} profit`;
  return `Down ${currency} ${Math.abs(amount).toFixed(2)}`;
}
