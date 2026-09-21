#ifndef SIGNALBRIDGE_BRIDGEEXECUTOR_MQH
#define SIGNALBRIDGE_BRIDGEEXECUTOR_MQH
#include <Trade/Trade.mqh>
#include <SignalBridge/Json.mqh>
// Executes one bridge command via CTrade. Retry/normalisation rules are those of mt5-copytrader's Replayer.

struct BridgeCommand
{
   string cmd_id; string type; string symbol; string side;
   double volume; double sl; double tp; string comment; double price; datetime expires_at;
   ulong position; ulong order;
};

struct BridgeResult
{
   string cmd_id; bool ok; uint retcode; string retcode_text;
   ulong position; ulong order; double fill_price; int attempts;
};

bool ParseCommand(const string json, BridgeCommand &c)
{
   c.cmd_id = ""; c.type = ""; c.symbol = ""; c.side = ""; c.volume = 0; c.sl = 0; c.tp = 0; c.comment = "";
   c.price = 0; c.expires_at = 0; c.position = 0; c.order = 0;
   if(!JsonGetString(json, "cmd_id", c.cmd_id) || c.cmd_id == "") return false;
   if(!JsonGetString(json, "type", c.type)) return false;
   if(c.type != "ping" && c.type != "open_market" && c.type != "open_pending" && c.type != "modify_sl" && c.type != "close" && c.type != "cancel") return false;
   string s; long l; double d;
   if(JsonGetString(json, "symbol", s)) c.symbol = s;
   if(JsonGetString(json, "side", s)) c.side = s;
   if(JsonGetString(json, "comment", s)) c.comment = s;
   if(JsonGetDouble(json, "volume", d)) c.volume = d;
   if(JsonGetDouble(json, "sl", d)) c.sl = d;
   if(JsonGetDouble(json, "tp", d)) c.tp = d;
   if(JsonGetDouble(json, "price", d)) c.price = d;
   if(JsonGetString(json, "expires_at", s)) c.expires_at = StringToTime(s);
   if(JsonGetLong(json, "position", l)) c.position = (ulong)l;
   if(JsonGetLong(json, "order", l)) c.order = (ulong)l;
   return true;
}

string ResultToJson(const BridgeResult &r)
{
   return "{\"cmd_id\":\"" + JsonEscape(r.cmd_id) + "\",\"ok\":" + (r.ok ? "true" : "false")
        + ",\"retcode\":" + IntegerToString(r.retcode)
        + ",\"retcode_text\":\"" + JsonEscape(r.retcode_text) + "\",\"position\":" + IntegerToString((long)r.position)
        + ",\"order\":" + IntegerToString((long)r.order) + ",\"fill_price\":" + JsonTrimZeros(DoubleToString(r.fill_price, 8))
        + ",\"attempts\":" + IntegerToString(r.attempts) + "}";
}

class CBridgeExecutor
{
private:
   CTrade m_trade;
   int    m_max_retries;
   string m_tag;
   // Set by a TryXxx() when it returns false WITHOUT calling any CTrade method this attempt
   // (a precondition check failed — symbol/position/order not found, or an invalid side). Cleared
   // before every attempt. When set, Execute() must not read m_trade's state: it would be leftover
   // from a PREVIOUS, unrelated command and misreport this attempt (e.g. a modify_sl or close whose
   // position genuinely has not appeared in the local position array yet — common on a live/demo
   // connection — would otherwise be reported using the last successful command's own "done" text).
   string m_precond_fail;

   bool Accepted()
   {
      uint rc = m_trade.ResultRetcode();
      return (rc == TRADE_RETCODE_DONE || rc == TRADE_RETCODE_DONE_PARTIAL || rc == TRADE_RETCODE_PLACED);
   }
   bool IsRetryable(const uint rc)
   {
      switch(rc)
      {
         case 0: case TRADE_RETCODE_REQUOTE: case TRADE_RETCODE_REJECT: case TRADE_RETCODE_ERROR: case TRADE_RETCODE_TIMEOUT:
         case TRADE_RETCODE_PRICE_CHANGED: case TRADE_RETCODE_PRICE_OFF: case TRADE_RETCODE_TOO_MANY_REQUESTS:
         case TRADE_RETCODE_LOCKED: case TRADE_RETCODE_CONNECTION: case TRADE_RETCODE_DONE_PARTIAL:
            return true;
      }
      return false;
   }
   double NormalizeStop(const string symbol, const double price)
   {
      if(price <= 0) return 0.0;
      return NormalizeDouble(price, (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS));
   }
   void Fill(BridgeResult &r, const bool ok)
   {
      r.ok = ok; r.retcode = m_trade.ResultRetcode(); r.retcode_text = m_trade.ResultRetcodeDescription();
      r.order = m_trade.ResultOrder(); r.fill_price = m_trade.ResultPrice();
      r.position = 0;
      ulong deal = m_trade.ResultDeal();
      if(deal > 0 && HistoryDealSelect(deal)) r.position = (ulong)HistoryDealGetInteger(deal, DEAL_POSITION_ID);
      else if(ok && r.order > 0 && PositionSelectByTicket(r.order)) r.position = r.order;
   }

   // One attempt of each type. Returns true when accepted.
   bool TryOpenMarket(const BridgeCommand &c)
   {
      if(c.side != "BUY" && c.side != "SELL") { m_precond_fail = "invalid side '" + c.side + "'"; return false; }
      if(!SymbolSelect(c.symbol, true)) { m_precond_fail = "symbol '" + c.symbol + "' not found"; return false; }
      bool is_buy = (c.side == "BUY");
      double price = is_buy ? SymbolInfoDouble(c.symbol, SYMBOL_ASK) : SymbolInfoDouble(c.symbol, SYMBOL_BID);
      m_trade.SetTypeFillingBySymbol(c.symbol);
      m_trade.PositionOpen(c.symbol, is_buy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL, c.volume, price,
                           NormalizeStop(c.symbol, c.sl), NormalizeStop(c.symbol, c.tp), c.comment);
      return Accepted();
   }
   bool TryOpenPending(const BridgeCommand &c)
   {
      if(c.side != "BUY" && c.side != "SELL") { m_precond_fail = "invalid side '" + c.side + "'"; return false; }
      if(!SymbolSelect(c.symbol, true)) { m_precond_fail = "symbol '" + c.symbol + "' not found"; return false; }
      m_trade.SetTypeFillingBySymbol(c.symbol);
      ENUM_ORDER_TYPE_TIME tt = (c.expires_at > 0) ? ORDER_TIME_SPECIFIED : ORDER_TIME_GTC;
      double px = NormalizeStop(c.symbol, c.price), sl = NormalizeStop(c.symbol, c.sl), tp = NormalizeStop(c.symbol, c.tp);
      if(c.side == "BUY") m_trade.BuyLimit(c.volume, px, c.symbol, sl, tp, tt, c.expires_at, c.comment);
      else                m_trade.SellLimit(c.volume, px, c.symbol, sl, tp, tt, c.expires_at, c.comment);
      return Accepted();
   }
   bool TryModifySl(const BridgeCommand &c)
   {
      if(!PositionSelectByTicket(c.position)) { m_precond_fail = "position #" + IntegerToString((long)c.position) + " not found (yet)"; return false; }
      string sym = PositionGetString(POSITION_SYMBOL);
      double cur_tp = PositionGetDouble(POSITION_TP), cur_sl = PositionGetDouble(POSITION_SL);
      double sl = NormalizeStop(sym, c.sl);
      if(MathAbs(cur_sl - sl) < 1e-9) return true;   // no-op is success
      m_trade.PositionModify(c.position, sl, cur_tp);
      return Accepted() || m_trade.ResultRetcode() == TRADE_RETCODE_NO_CHANGES;
   }
   bool TryClose(const BridgeCommand &c)
   {
      if(!PositionSelectByTicket(c.position)) { m_precond_fail = "position #" + IntegerToString((long)c.position) + " not found (yet)"; return false; }
      m_trade.PositionClose(c.position);
      if(!Accepted()) return false;
      // A DONE_PARTIAL retcode is the trade server's own word that volume remains open — retry.
      // Do NOT re-read the position array here: on a live/demo connection the terminal's local
      // position cache can still show the (about-to-vanish) position for a moment after a genuine
      // DONE close, which wrongly looked like "still open" and reported ok=false for a close that
      // had already fully succeeded (seen live: retcode 10009 DONE with ok:false).
      return (m_trade.ResultRetcode() != TRADE_RETCODE_DONE_PARTIAL);
   }
   bool TryCancel(const BridgeCommand &c)
   {
      if(!OrderSelect(c.order)) { m_precond_fail = "order #" + IntegerToString((long)c.order) + " not found (yet)"; return false; }
      m_trade.OrderDelete(c.order);
      return Accepted();
   }

public:
   void Init(const ulong magic, const int deviation_points, const int max_retries)
   {
      m_max_retries = (max_retries < 1) ? 1 : max_retries;
      m_tag = "[SignalBridge] ";
      m_trade.SetExpertMagicNumber(magic);
      m_trade.SetDeviationInPoints(deviation_points);
      m_trade.SetAsyncMode(false);
      m_trade.LogLevel(LOG_LEVEL_ERRORS);
   }

   void Execute(const BridgeCommand &c, BridgeResult &r)
   {
      r.cmd_id = c.cmd_id; r.ok = false; r.retcode = 0; r.retcode_text = ""; r.position = 0; r.order = 0; r.fill_price = 0; r.attempts = 0;
      if(c.type == "ping") { r.ok = true; r.retcode_text = "pong"; return; }
      for(int attempt = 1; attempt <= m_max_retries; attempt++)
      {
         r.attempts = attempt;
         m_precond_fail = "";
         bool ok = false;
         if(c.type == "open_market")       ok = TryOpenMarket(c);
         else if(c.type == "open_pending") ok = TryOpenPending(c);
         else if(c.type == "modify_sl")    ok = TryModifySl(c);
         else if(c.type == "close")        ok = TryClose(c);
         else if(c.type == "cancel")       ok = TryCancel(c);
         if(m_precond_fail != "")
         {
            // No CTrade call happened this attempt — m_trade's state belongs to a previous command.
            // retcode 0 is always retryable, so a "not found yet" precondition gets another chance
            // to let the position/order catch up before this event is journaled as failed.
            r.ok = false; r.retcode = 0; r.retcode_text = m_precond_fail; r.position = 0; r.order = 0; r.fill_price = 0;
         }
         else
         {
            Fill(r, ok);
         }
         if(ok)
         {
            if(c.type == "close" || c.type == "modify_sl") { r.position = c.position; r.order = 0; }
            if(c.type == "cancel") { r.order = c.order; r.position = 0; }
            return;
         }
         if(!IsRetryable(r.retcode)) break;
         Print(m_tag, c.type, " ", c.cmd_id, " attempt ", attempt, "/", m_max_retries, " failed retcode=", r.retcode, " ", r.retcode_text);
         if(attempt < m_max_retries) Sleep(300 * attempt);
      }
      if(r.retcode_text == "") r.retcode_text = "precondition failed (symbol/position/order not found or invalid side)";
   }
};
#endif
