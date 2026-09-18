#ifndef SIGNALBRIDGE_BRIDGESTATE_MQH
#define SIGNALBRIDGE_BRIDGESTATE_MQH
#include <SignalBridge/JsonOut.mqh>
// Snapshot of the account for the Python side. Written atomically: state.tmp then FileMove → state.json.

string TimeStr(const datetime t) { return TimeToString(t, TIME_DATE|TIME_SECONDS); }

string PositionTypeName(const long t) { return (t == POSITION_TYPE_BUY) ? "BUY" : "SELL"; }

string OrderTypeName(const long t)
{
   switch((int)t)
   {
      case ORDER_TYPE_BUY: return "BUY";                 case ORDER_TYPE_SELL: return "SELL";
      case ORDER_TYPE_BUY_LIMIT: return "BUY_LIMIT";     case ORDER_TYPE_SELL_LIMIT: return "SELL_LIMIT";
      case ORDER_TYPE_BUY_STOP: return "BUY_STOP";       case ORDER_TYPE_SELL_STOP: return "SELL_STOP";
      case ORDER_TYPE_BUY_STOP_LIMIT: return "BUY_STOP_LIMIT"; case ORDER_TYPE_SELL_STOP_LIMIT: return "SELL_STOP_LIMIT";
      case ORDER_TYPE_CLOSE_BY: return "CLOSE_BY";
   }
   return "UNKNOWN";
}

string DealEntryName(const long e)
{
   switch((int)e) { case DEAL_ENTRY_IN: return "IN"; case DEAL_ENTRY_OUT: return "OUT"; case DEAL_ENTRY_INOUT: return "INOUT"; case DEAL_ENTRY_OUT_BY: return "OUT_BY"; }
   return "UNKNOWN";
}

string DealReasonName(const long r)
{
   switch((int)r)
   {
      case DEAL_REASON_CLIENT: return "CLIENT";   case DEAL_REASON_MOBILE: return "MOBILE";  case DEAL_REASON_WEB: return "WEB";
      case DEAL_REASON_EXPERT: return "EXPERT";   case DEAL_REASON_SL: return "SL";          case DEAL_REASON_TP: return "TP";
      case DEAL_REASON_SO: return "SO";           case DEAL_REASON_ROLLOVER: return "ROLLOVER"; case DEAL_REASON_VMARGIN: return "VMARGIN";
      case DEAL_REASON_SPLIT: return "SPLIT";
   }
   return "OTHER";
}

bool WriteStateJson(const string dir, const string &symbols[], const ulong magic)
{
   CJsonOut j;
   j.BeginObject();
   j.Key("ts"); j.Str(TimeStr(TimeLocal()));
   j.Key("account"); j.BeginObject();
      j.Key("login");       j.Int(AccountInfoInteger(ACCOUNT_LOGIN));
      j.Key("balance");     j.Num(AccountInfoDouble(ACCOUNT_BALANCE));
      j.Key("equity");      j.Num(AccountInfoDouble(ACCOUNT_EQUITY));
      j.Key("margin_free"); j.Num(AccountInfoDouble(ACCOUNT_MARGIN_FREE));
      j.Key("hedging");     j.Bool(AccountInfoInteger(ACCOUNT_MARGIN_MODE) == ACCOUNT_MARGIN_MODE_RETAIL_HEDGING);
   j.EndObject();
   j.Key("symbols"); j.BeginObject();
   for(int i = 0; i < ArraySize(symbols); i++)
   {
      string s = symbols[i];
      if(!SymbolSelect(s, true)) continue;
      j.Key(s); j.BeginObject();
         j.Key("bid");           j.Num(SymbolInfoDouble(s, SYMBOL_BID));
         j.Key("ask");           j.Num(SymbolInfoDouble(s, SYMBOL_ASK));
         j.Key("digits");        j.Int(SymbolInfoInteger(s, SYMBOL_DIGITS));
         j.Key("point");         j.Num(SymbolInfoDouble(s, SYMBOL_POINT));
         j.Key("volume_step");   j.Num(SymbolInfoDouble(s, SYMBOL_VOLUME_STEP));
         j.Key("volume_min");    j.Num(SymbolInfoDouble(s, SYMBOL_VOLUME_MIN));
         j.Key("volume_max");    j.Num(SymbolInfoDouble(s, SYMBOL_VOLUME_MAX));
         j.Key("tick_value");    j.Num(SymbolInfoDouble(s, SYMBOL_TRADE_TICK_VALUE));
         j.Key("tick_size");     j.Num(SymbolInfoDouble(s, SYMBOL_TRADE_TICK_SIZE));
         j.Key("trade_allowed"); j.Bool(SymbolInfoInteger(s, SYMBOL_TRADE_MODE) == SYMBOL_TRADE_MODE_FULL && TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) && MQLInfoInteger(MQL_TRADE_ALLOWED));
      j.EndObject();
   }
   j.EndObject();
   j.Key("positions"); j.BeginArray();
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      j.BeginObject();
         j.Key("ticket");     j.Int((long)ticket);
         j.Key("symbol");     j.Str(PositionGetString(POSITION_SYMBOL));
         j.Key("type");       j.Str(PositionTypeName(PositionGetInteger(POSITION_TYPE)));
         j.Key("volume");     j.Num(PositionGetDouble(POSITION_VOLUME));
         j.Key("price_open"); j.Num(PositionGetDouble(POSITION_PRICE_OPEN));
         j.Key("sl");         j.Num(PositionGetDouble(POSITION_SL));
         j.Key("tp");         j.Num(PositionGetDouble(POSITION_TP));
         j.Key("comment");    j.Str(PositionGetString(POSITION_COMMENT));
         j.Key("magic");      j.Int(PositionGetInteger(POSITION_MAGIC));
      j.EndObject();
   }
   j.EndArray();
   j.Key("orders"); j.BeginArray();
   for(int i = 0; i < OrdersTotal(); i++)
   {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0) continue;
      j.BeginObject();
         j.Key("ticket");     j.Int((long)ticket);
         j.Key("symbol");     j.Str(OrderGetString(ORDER_SYMBOL));
         j.Key("type");       j.Str(OrderTypeName(OrderGetInteger(ORDER_TYPE)));
         j.Key("volume");     j.Num(OrderGetDouble(ORDER_VOLUME_CURRENT));
         j.Key("price");      j.Num(OrderGetDouble(ORDER_PRICE_OPEN));
         j.Key("sl");         j.Num(OrderGetDouble(ORDER_SL));
         j.Key("tp");         j.Num(OrderGetDouble(ORDER_TP));
         j.Key("comment");    j.Str(OrderGetString(ORDER_COMMENT));
         j.Key("expiration"); j.Str(TimeStr((datetime)OrderGetInteger(ORDER_TIME_EXPIRATION)));
      j.EndObject();
   }
   j.EndArray();
   j.Key("deals_recent"); j.BeginArray();
   if(HistorySelect(TimeCurrent() - 86400, TimeCurrent() + 3600))
   {
      int total = HistoryDealsTotal();
      int first = (total > 200) ? total - 200 : 0;
      for(int i = first; i < total; i++)
      {
         ulong d = HistoryDealGetTicket(i);
         if(d == 0) continue;
         long type = HistoryDealGetInteger(d, DEAL_TYPE);
         if(type != DEAL_TYPE_BUY && type != DEAL_TYPE_SELL) continue;
         j.BeginObject();
            j.Key("ticket");      j.Int((long)d);
            j.Key("position_id"); j.Int(HistoryDealGetInteger(d, DEAL_POSITION_ID));
            j.Key("entry");       j.Str(DealEntryName(HistoryDealGetInteger(d, DEAL_ENTRY)));
            j.Key("reason");      j.Str(DealReasonName(HistoryDealGetInteger(d, DEAL_REASON)));
            j.Key("price");       j.Num(HistoryDealGetDouble(d, DEAL_PRICE));
            j.Key("volume");      j.Num(HistoryDealGetDouble(d, DEAL_VOLUME));
            j.Key("time");        j.Str(TimeStr((datetime)HistoryDealGetInteger(d, DEAL_TIME)));
         j.EndObject();
      }
   }
   j.EndArray();
   j.EndObject();

   string tmp = dir + "/state.tmp", dest = dir + "/state.json";
   int h = FileOpen(tmp, FILE_WRITE|FILE_BIN);
   if(h == INVALID_HANDLE) { Print("WriteStateJson: cannot open ", tmp, " error=", GetLastError()); return false; }
   uchar bytes[]; StringToCharArray(j.Text(), bytes, 0, -1, CP_UTF8);
   int n = ArraySize(bytes); if(n > 0 && bytes[n - 1] == 0) n--;
   FileWriteArray(h, bytes, 0, n);
   FileClose(h);
   if(!FileMove(tmp, 0, dest, FILE_REWRITE)) { Print("WriteStateJson: FileMove failed error=", GetLastError()); return false; }
   return true;
}
#endif
