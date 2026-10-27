#ifndef COPYTRADER_SOURCEHANDLER_MQH
#define COPYTRADER_SOURCEHANDLER_MQH
// Turns the source account's trade transactions into events appended to today's events file.
// Tracks open positions (volume/SL/TP/symbol/direction, keyed by POSITION_IDENTIFIER) so CLOSE vs
// PARTIAL_CLOSE and MODIFY dedupe never depend on the (possibly not-yet-updated) position state at
// the time the deal arrives. The tracker is persisted to <base>_source_positions.txt in the source
// terminal's own MQL5\Files so a restart knows what the destination has already been told, and
// Reconcile() compares it against the live positions to emit whatever a missed transaction (terminal
// disconnected, EA restarted, transaction queue overflow) would otherwise have lost.
//
// Concurrency: MQL5 runs an EA's event handlers (OnTradeTransaction, OnTimer, OnInit, ...) strictly
// one at a time on the EA's own thread, so Reconcile() never interleaves with OnDeal().
//
// Tracker updates happen only AFTER the event line was written: if the append fails, the tracker
// still differs from the live state and the next Reconcile() sweep re-emits the event.
//
// Sweep vs. transaction double-counting is prevented three ways: (1) the sweep only acts on a
// difference it has seen on two consecutive sweeps (the position list can lag a deal for a moment);
// (2) a gone position is only reported closed when the deal history's net open volume (IN - OUT) is
// less than what the tracker holds - a consistent tracker never closes on a transient absence; (3) each
// entry remembers the POSITION_TIME_UPDATE_MSC the sweep last synced against, and an OUT deal at or
// before that time is ignored because the sweep already reported it. That stamp is ONLY ever taken
// from the position (by the sweep, after the settle window, so any later deal is >= 2 s newer) -
// never from a deal's own time, which would make a second deal in the same millisecond disappear.
#include <CopyTrader/Event.mqh>
#include <CopyTrader/EventLog.mqh>

#define RECONCILE_SETTLE_SEC     2 // a position changed more recently than this may still have its transaction in flight
#define RECONCILE_MISSING_SWEEPS 2 // a tracked position must be absent on this many consecutive sweeps before CLOSE is emitted
#define RECONCILE_VOLUME_SWEEPS  2 // a volume difference must be seen unchanged on this many consecutive sweeps before it is emitted

class CSourceHandler
{
private:
   string m_base;
   string m_tag;
   int    m_seq;
   // Tracker (parallel arrays, one entry per open source position).
   long   m_pos_ids[];
   double m_volumes[];
   double m_sls[];
   double m_tps[];
   string m_symbols[];
   string m_dirs[];           // "BUY" / "SELL"
   long   m_synced_msc[];     // POSITION_TIME_UPDATE_MSC the sweep last synced against (persisted); late-DEAL_ADD guard only
   int    m_missing[];        // consecutive sweeps on which the position was not found live (memory only)
   double m_pending_volume[]; // live volume seen differing from the tracker on the last sweep (memory only)
   int    m_pending_count[];  // consecutive sweeps that same differing volume was seen (memory only)
   bool   m_keep_logged[];    // "not visible but no closing deal" already logged for this entry (memory only)
   datetime m_last_not_connected_log;

   int Find(const long pos_id)
   {
      for(int i = 0; i < ArraySize(m_pos_ids); i++) if(m_pos_ids[i] == pos_id) return i;
      return -1;
   }

   void Resize(const int n)
   {
      ArrayResize(m_pos_ids, n);
      ArrayResize(m_volumes, n);
      ArrayResize(m_sls, n);
      ArrayResize(m_tps, n);
      ArrayResize(m_symbols, n);
      ArrayResize(m_dirs, n);
      ArrayResize(m_synced_msc, n);
      ArrayResize(m_missing, n);
      ArrayResize(m_pending_volume, n);
      ArrayResize(m_pending_count, n);
      ArrayResize(m_keep_logged, n);
   }

   void ClearSweepState(const int i)
   {
      m_missing[i] = 0;
      m_pending_volume[i] = 0;
      m_pending_count[i] = 0;
      m_keep_logged[i] = false;
   }

   void Track(const long pos_id, const double volume, const double sl, const double tp, const string symbol, const string dir)
   {
      int i = Find(pos_id);
      if(i < 0)
      {
         i = ArraySize(m_pos_ids);
         Resize(i + 1);
      }
      m_pos_ids[i] = pos_id;
      m_volumes[i] = volume;
      m_sls[i] = sl;
      m_tps[i] = tp;
      m_symbols[i] = symbol;
      m_dirs[i] = dir;
      m_synced_msc[i] = 0;
      ClearSweepState(i);
      SaveTracker();
   }

   // synced_msc <= 0 leaves the entry's sync time unchanged (0 is "never synced", never a reset target).
   void Update(const int i, const double volume, const double sl, const double tp, const long synced_msc = -1)
   {
      m_volumes[i] = volume;
      m_sls[i] = sl;
      m_tps[i] = tp;
      if(synced_msc > 0) m_synced_msc[i] = synced_msc;
      SaveTracker();
   }

   void Untrack(const long pos_id)
   {
      int i = Find(pos_id);
      if(i < 0) return;
      int n = ArraySize(m_pos_ids);
      for(int k = i; k < n - 1; k++)
      {
         m_pos_ids[k] = m_pos_ids[k + 1];
         m_volumes[k] = m_volumes[k + 1];
         m_sls[k] = m_sls[k + 1];
         m_tps[k] = m_tps[k + 1];
         m_symbols[k] = m_symbols[k + 1];
         m_dirs[k] = m_dirs[k + 1];
         m_synced_msc[k] = m_synced_msc[k + 1];
         m_missing[k] = m_missing[k + 1];
         m_pending_volume[k] = m_pending_volume[k + 1];
         m_pending_count[k] = m_pending_count[k + 1];
         m_keep_logged[k] = m_keep_logged[k + 1];
      }
      Resize(n - 1);
      SaveTracker();
   }

   // The position's magic number and comment: from the live position, else from its opening deal in
   // the history (a closed position). 0 / "" when neither can be found. Readers use them to tell
   // hand-placed trades from EA or copy-service trades.
   void PositionOrigin(const long pos_id, long &magic, string &comment)
   {
      magic = 0;
      comment = "";
      if(PositionSelectByTicket((ulong)pos_id))
      {
         magic = PositionGetInteger(POSITION_MAGIC);
         comment = PositionGetString(POSITION_COMMENT);
      }
      else if(HistorySelectByPosition(pos_id))
      {
         for(int d = 0; d < HistoryDealsTotal(); d++)
         {
            ulong t = HistoryDealGetTicket(d);
            if(t == 0 || HistoryDealGetInteger(t, DEAL_ENTRY) != DEAL_ENTRY_IN) continue;
            magic = HistoryDealGetInteger(t, DEAL_MAGIC);
            comment = HistoryDealGetString(t, DEAL_COMMENT);
            break;
         }
      }
      // The events file is one JSON object per line and Json.mqh does not escape control characters.
      StringReplace(comment, "\r", " ");
      StringReplace(comment, "\n", " ");
      StringReplace(comment, "\t", " ");
   }

   // Appends the event to today's file. `prefix` ("" or "reconcile: ") tags the log line with where
   // the event came from. Returns false when the line could not be written (already logged).
   bool Emit(CopyEvent &ev, const string prefix)
   {
      m_seq++;
      ev.event_id = MakeEventId(TimeLocal(), m_seq);
      ev.ts = TimeToString(TimeLocal(), TIME_DATE|TIME_SECONDS);
      ev.source_balance = AccountInfoDouble(ACCOUNT_BALANCE);
      ev.login = AccountInfoInteger(ACCOUNT_LOGIN);
      PositionOrigin(ev.source_pos_id, ev.magic, ev.comment);
      string line = EventToJson(ev);
      string file = CurrentFile();
      if(!AppendLineCommon(file, line))
      {
         Print(m_tag, prefix, "FAILED to write event to ", file, " - will be re-emitted by reconcile if the position state still differs: ", line);
         return false;
      }
      Print(m_tag, prefix, ev.type, " ", ev.symbol, " pos=", ev.source_pos_id, " vol=", ev.volume, " -> ", file);
      return true;
   }

   string DirectionOfSelected() { return (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? "BUY" : "SELL"; }

   void OnDeal(const MqlTradeTransaction &trans)
   {
      ulong deal = trans.deal;
      ENUM_DEAL_TYPE  dtype;
      ENUM_DEAL_ENTRY entry;
      long   pos_id;
      long   deal_msc = 0;   // 0 = unknown (transaction fallback)
      double volume, price;
      string symbol;

      bool in_history = HistoryDealSelect(deal);
      if(!in_history)
      {
         // The deal may not have reached the terminal's history cache yet: refresh it and retry once.
         HistorySelect(TimeCurrent() - 86400, TimeCurrent() + 3600);
         in_history = HistoryDealSelect(deal);
      }
      if(in_history)
      {
         dtype  = (ENUM_DEAL_TYPE)HistoryDealGetInteger(deal, DEAL_TYPE);
         entry  = (ENUM_DEAL_ENTRY)HistoryDealGetInteger(deal, DEAL_ENTRY);
         pos_id = HistoryDealGetInteger(deal, DEAL_POSITION_ID);
         volume = HistoryDealGetDouble(deal, DEAL_VOLUME);
         price  = HistoryDealGetDouble(deal, DEAL_PRICE);
         symbol = HistoryDealGetString(deal, DEAL_SYMBOL);
         deal_msc = HistoryDealGetInteger(deal, DEAL_TIME_MSC);
      }
      else
      {
         // Fall back to the transaction's own fields. It carries no entry type, so infer it: a deal on a
         // position that is gone, or on a tracked position whose live volume shrank, is a closing deal.
         dtype  = trans.deal_type;
         pos_id = (long)trans.position;
         volume = trans.volume;
         price  = trans.price;
         symbol = trans.symbol;
         int  ti = Find(pos_id);
         bool selectable = PositionSelectByTicket((ulong)pos_id);
         bool closing = !selectable || (ti >= 0 && PositionGetDouble(POSITION_VOLUME) < m_volumes[ti] - 1e-8);
         entry = closing ? DEAL_ENTRY_OUT : DEAL_ENTRY_IN;
         Print(m_tag, "deal #", deal, " not in history yet - using transaction fields (", EnumToString(entry),
               " ", volume, " on position ", pos_id, ")");
      }
      if(dtype != DEAL_TYPE_BUY && dtype != DEAL_TYPE_SELL) return;   // balance ops, credits, etc.

      CopyEvent ev;
      EventClear(ev);
      ev.source_pos_id = pos_id;
      ev.symbol = symbol;
      ev.volume = volume;
      ev.price = price;

      if(entry == DEAL_ENTRY_IN)
      {
         ev.type = EVT_OPEN;
         ev.direction = (dtype == DEAL_TYPE_BUY) ? "BUY" : "SELL";
         bool   selectable = PositionSelectByTicket((ulong)pos_id);
         double live_vol = 0;
         if(selectable)
         {
            ev.sl = PositionGetDouble(POSITION_SL);
            ev.tp = PositionGetDouble(POSITION_TP);
            live_vol = PositionGetDouble(POSITION_VOLUME);
         }
         int existing = Find(pos_id);
         // Emit the OPEN as-is even for an additional fill: the destination dedupes repeat OPENs on
         // the same position (and keeps the first fill's size).
         if(!Emit(ev, "")) return;
         if(existing < 0)
         {
            Track(pos_id, volume, ev.sl, ev.tp, symbol, ev.direction);
            return;
         }
         if(selectable)
         {
            // Live volume is the truth (a repeated/duplicated DEAL_ADD must not inflate the tracker).
            Update(existing, live_vol, ev.sl, ev.tp);
            Print(m_tag, "additional fill ", volume, " on position ", pos_id, " (live total ", live_vol, ") - destination will keep the first fill's size");
         }
         else
         {
            Update(existing, m_volumes[existing] + volume, ev.sl, ev.tp);
            Print(m_tag, "additional fill ", volume, " on position ", pos_id, " (accumulated ", m_volumes[existing], ", position not selectable) - destination will keep the first fill's size");
         }
         return;
      }
      if(entry == DEAL_ENTRY_OUT || entry == DEAL_ENTRY_OUT_BY)
      {
         // A closing deal trades against the position, so flip to report the position's direction.
         ev.direction = (dtype == DEAL_TYPE_BUY) ? "SELL" : "BUY";
         int i = Find(pos_id);
         if(i >= 0 && deal_msc > 0 && deal_msc <= m_synced_msc[i])
         {
            // The sweep already synced the tracker against a position state that includes this deal
            // (late DEAL_ADD delivery): it was reported as a reconcile event, do not report it twice.
            Print(m_tag, "deal #", deal, " already reflected by reconcile - ignored");
            return;
         }
         if(i < 0)
         {
            // Never saw this position open (started mid-life / missed a transaction): trust the live state.
            bool   live = (PositionSelectByTicket((ulong)pos_id) && PositionGetDouble(POSITION_VOLUME) > 1e-8);
            double live_vol = live ? PositionGetDouble(POSITION_VOLUME) : 0;
            double live_sl  = live ? PositionGetDouble(POSITION_SL) : 0;
            double live_tp  = live ? PositionGetDouble(POSITION_TP) : 0;
            ev.type = live ? EVT_PARTIAL_CLOSE : EVT_CLOSE;
            ev.remaining = live ? NormalizeDouble(live_vol, 8) : 0;
            if(!Emit(ev, "")) return;
            if(live)
            {
               Track(pos_id, live_vol, live_sl, live_tp, symbol, ev.direction);
               Print(m_tag, "untracked position ", pos_id, " partially closed; now tracking remaining ", live_vol);
            }
            return;
         }
         double before = m_volumes[i];
         double remaining = before - volume;
         // The tracker exists so a still-open position (racy post-deal state) never turns a
         // partial close into a full close. But a position that is ABSENT after an OUT deal is
         // authoritative evidence that it fully closed - absence cannot be a race, so trust that
         // over tracker arithmetic (which can be stale/inflated, e.g. after a duplicated fill).
         bool live_gone = !PositionSelectByTicket((ulong)pos_id);
         bool full = (remaining < 1e-8 || live_gone);
         if(full)
         {
            if(live_gone && remaining >= 1e-8)
               Print(m_tag, "position ", pos_id, " is gone after closing ", volume, " but tracker expected ", remaining, " remaining - treating as CLOSE");
            ev.type = EVT_CLOSE;
            ev.remaining = 0;
         }
         else
         {
            ev.type = EVT_PARTIAL_CLOSE;
            ev.remaining = NormalizeDouble(remaining, 8);
         }
         if(!Emit(ev, "")) return;
         if(full) Untrack(pos_id);
         else     Update(i, remaining, m_sls[i], m_tps[i]);   // sync stamp untouched: it belongs to the sweep only
         return;
      }
      Print(m_tag, "unsupported deal entry ", EnumToString(entry), " on position ", pos_id, " (netting reversal?) - ignored");
   }

   void OnPositionChange(const long pos_id, const string symbol, const double sl, const double tp)
   {
      int i = Find(pos_id);
      if(i >= 0 && MathAbs(m_sls[i] - sl) < 1e-9 && MathAbs(m_tps[i] - tp) < 1e-9) return;   // nothing we mirror changed
      CopyEvent ev;
      EventClear(ev);
      ev.type = EVT_MODIFY;
      ev.source_pos_id = pos_id;
      ev.symbol = (symbol != "" || i < 0) ? symbol : m_symbols[i];
      ev.sl = sl;
      ev.tp = tp;
      if(i >= 0)
         ev.direction = m_dirs[i];
      else if(PositionSelectByTicket((ulong)pos_id))
         ev.direction = DirectionOfSelected();
      if(!Emit(ev, "")) return;
      if(i >= 0) Update(i, m_volumes[i], sl, tp);
   }

   bool InLive(const long &live_ids[], const long pos_id)
   {
      for(int i = 0; i < ArraySize(live_ids); i++) if(live_ids[i] == pos_id) return true;
      return false;
   }

public:
   void Init(const string base)
   {
      m_base = base;
      m_tag = "[CopierSource] ";
      m_seq = 0;
      m_last_not_connected_log = 0;
      Resize(0);
   }

   string CurrentFile()     { return EventsFileName(m_base, TimeLocal()); }
   string TrackerFileName() { return m_base + "_source_positions.txt"; }
   int    TrackedCount()    { return ArraySize(m_pos_ids); }
   double TrackedVolume(const long pos_id) { int i = Find(pos_id); return (i < 0) ? -1.0 : m_volumes[i]; }
   long   SyncedMsc(const long pos_id)     { int i = Find(pos_id); return (i < 0) ? -1 : m_synced_msc[i]; }

   // One line per tracked position: pos_id=volume,sl,tp,symbol,dir,synced_msc (source terminal's own
   // MQL5\Files). LoadTracker also accepts the 5-field form without synced_msc.
   bool SaveTracker()
   {
      int h = FileOpen(TrackerFileName(), FILE_WRITE|FILE_TXT|FILE_ANSI);
      if(h == INVALID_HANDLE)
      {
         Print(m_tag, "cannot write tracker ", TrackerFileName(), " error=", GetLastError());
         return false;
      }
      for(int i = 0; i < ArraySize(m_pos_ids); i++)
         FileWrite(h, IntegerToString(m_pos_ids[i]) + "=" + DoubleToString(m_volumes[i], 8) + "," + DoubleToString(m_sls[i], 8)
                      + "," + DoubleToString(m_tps[i], 8) + "," + m_symbols[i] + "," + m_dirs[i]
                      + "," + IntegerToString(m_synced_msc[i]));
      FileClose(h);
      return true;
   }

   // Replaces the in-memory tracker with the persisted one. False (tracker left empty) when there is
   // no file or it cannot be read; malformed lines are skipped with a log line.
   bool LoadTracker()
   {
      Resize(0);
      if(!FileIsExist(TrackerFileName())) return false;
      int h = FileOpen(TrackerFileName(), FILE_READ|FILE_TXT|FILE_ANSI);
      if(h == INVALID_HANDLE)
      {
         Print(m_tag, "tracker ", TrackerFileName(), " exists but cannot be read, error=", GetLastError());
         return false;
      }
      while(!FileIsEnding(h))
      {
         string line = FileReadString(h);
         StringTrimRight(line);
         if(line == "") continue;
         int eq = StringFind(line, "=");
         string fields[];
         int nf = (eq <= 0) ? 0 : StringSplit(StringSubstr(line, eq + 1), ',', fields);
         if(nf != 5 && nf != 6)
         {
            Print(m_tag, "tracker line skipped (malformed): ", line);
            continue;
         }
         int i = ArraySize(m_pos_ids);
         Resize(i + 1);
         m_pos_ids[i] = StringToInteger(StringSubstr(line, 0, eq));
         m_volumes[i] = StringToDouble(fields[0]);
         m_sls[i]     = StringToDouble(fields[1]);
         m_tps[i]     = StringToDouble(fields[2]);
         m_symbols[i] = fields[3];
         m_dirs[i]    = fields[4];
         m_synced_msc[i] = (nf == 6) ? StringToInteger(fields[5]) : 0;
         ClearSweepState(i);
      }
      FileClose(h);
      return true;
   }

   // Remember volumes/SL/TP of positions already open so later closes are classified correctly
   // (no events emitted - for a first start without a tracker file).
   void SeedFromOpenPositions()
   {
      for(int i = 0; i < PositionsTotal(); i++)
      {
         ulong ticket = PositionGetTicket(i);
         if(ticket == 0) continue;
         Track(PositionGetInteger(POSITION_IDENTIFIER), PositionGetDouble(POSITION_VOLUME),
               PositionGetDouble(POSITION_SL), PositionGetDouble(POSITION_TP),
               PositionGetString(POSITION_SYMBOL), DirectionOfSelected());
      }
   }

   bool Connected() { return (bool)TerminalInfoInteger(TERMINAL_CONNECTED); }

   // Net open volume of pos_id according to the deal history (sum of IN deals minus sum of OUT/OUT_BY
   // deals), or -1 when the history is unavailable (cannot be selected, or holds no deal at all for
   // the position - e.g. not yet re-synced). last_out_price receives the price of the last closing deal.
   double HistoryNetVolume(const long pos_id, double &last_out_price)
   {
      last_out_price = 0;
      if(!HistorySelectByPosition(pos_id)) return -1;
      double net = 0;
      int seen = 0;
      for(int d = 0; d < HistoryDealsTotal(); d++)
      {
         ulong t = HistoryDealGetTicket(d);
         if(t == 0) continue;
         ENUM_DEAL_TYPE dtype = (ENUM_DEAL_TYPE)HistoryDealGetInteger(t, DEAL_TYPE);
         if(dtype != DEAL_TYPE_BUY && dtype != DEAL_TYPE_SELL) continue;
         ENUM_DEAL_ENTRY entry = (ENUM_DEAL_ENTRY)HistoryDealGetInteger(t, DEAL_ENTRY);
         if(entry == DEAL_ENTRY_IN)
            net += HistoryDealGetDouble(t, DEAL_VOLUME);
         else if(entry == DEAL_ENTRY_OUT || entry == DEAL_ENTRY_OUT_BY)
         {
            net -= HistoryDealGetDouble(t, DEAL_VOLUME);
            last_out_price = HistoryDealGetDouble(t, DEAL_PRICE);
         }
         else
            continue;   // INOUT (netting reversal) is not mirrored
         seen++;
      }
      if(seen == 0) return -1;
      return NormalizeDouble(net, 8);
   }

   // Compares the live positions with the tracker and emits whatever a missed transaction would have
   // produced (each logged with a "reconcile:" prefix). Positions changed within the last
   // RECONCILE_SETTLE_SEC are left alone - their DEAL_ADD / POSITION transaction may still be in
   // flight and OnDeal will handle it. A volume difference must be seen on RECONCILE_VOLUME_SWEEPS
   // consecutive sweeps, and a tracked position must be missing on RECONCILE_MISSING_SWEEPS
   // consecutive sweeps AND have a closing deal in history, before it is acted on, for the same reason.
   void Reconcile()
   {
      if(!Connected())
      {
         // Right after a (re)start the position list is empty until the account has synchronised;
         // sweeping it would report every tracked position as closed. Not counted as a sweep.
         if(m_last_not_connected_log == 0 || TimeLocal() - m_last_not_connected_log >= 60)
         {
            m_last_not_connected_log = TimeLocal();
            Print(m_tag, "reconcile: terminal not connected - sweep skipped");
         }
         return;
      }
      datetime now = TimeCurrent();
      long live_ids[];
      ArrayResize(live_ids, 0);
      int total = PositionsTotal();
      for(int p = 0; p < total; p++)
      {
         ulong ticket = PositionGetTicket(p);
         if(ticket == 0) continue;
         long pos_id = PositionGetInteger(POSITION_IDENTIFIER);
         int  k = ArraySize(live_ids);
         ArrayResize(live_ids, k + 1);
         live_ids[k] = pos_id;
         int i = Find(pos_id);
         if(i >= 0) { m_missing[i] = 0; m_keep_logged[i] = false; }
         if(now - (datetime)PositionGetInteger(POSITION_TIME_UPDATE) < RECONCILE_SETTLE_SEC) continue;

         string symbol   = PositionGetString(POSITION_SYMBOL);
         string dir      = DirectionOfSelected();
         double live_vol = PositionGetDouble(POSITION_VOLUME);
         double live_sl  = PositionGetDouble(POSITION_SL);
         double live_tp  = PositionGetDouble(POSITION_TP);
         double price_open = PositionGetDouble(POSITION_PRICE_OPEN);
         double price_cur  = PositionGetDouble(POSITION_PRICE_CURRENT);
         long   update_msc = PositionGetInteger(POSITION_TIME_UPDATE_MSC);

         CopyEvent ev;
         EventClear(ev);
         ev.source_pos_id = pos_id;
         ev.symbol = symbol;
         ev.direction = dir;
         if(i < 0)
         {
            // Live but untracked: the OPEN was missed.
            ev.type = EVT_OPEN;
            ev.volume = live_vol;
            ev.price = price_open;
            ev.sl = live_sl;
            ev.tp = live_tp;
            if(Emit(ev, "reconcile: ")) Track(pos_id, live_vol, live_sl, live_tp, symbol, dir);
            continue;
         }
         double tracked = m_volumes[i];
         if(MathAbs(live_vol - tracked) > 1e-8)
         {
            // A volume difference is acted on only when the same live volume is seen on
            // RECONCILE_VOLUME_SWEEPS consecutive sweeps: the position list can briefly lag a deal
            // that OnDeal has already reported (or still will).
            if(m_pending_count[i] > 0 && MathAbs(m_pending_volume[i] - live_vol) < 1e-9)
               m_pending_count[i]++;
            else
            {
               m_pending_volume[i] = live_vol;
               m_pending_count[i] = 1;
            }
            if(m_pending_count[i] >= RECONCILE_VOLUME_SWEEPS)
            {
               if(live_vol < tracked)
               {
                  ev.type = EVT_PARTIAL_CLOSE;
                  ev.volume = NormalizeDouble(tracked - live_vol, 8);
                  ev.remaining = NormalizeDouble(live_vol, 8);
                  ev.price = price_cur;
                  ev.sl = m_sls[i];
                  ev.tp = m_tps[i];
               }
               else
               {
                  // Additional fill: the destination dedupes the OPEN and keeps the first fill's size.
                  ev.type = EVT_OPEN;
                  ev.volume = NormalizeDouble(live_vol - tracked, 8);
                  ev.price = price_open;
                  ev.sl = live_sl;
                  ev.tp = live_tp;
               }
               if(Emit(ev, "reconcile: "))
               {
                  Update(i, live_vol, m_sls[i], m_tps[i], update_msc);   // a late OUT deal at/before this time is already reported
                  m_pending_count[i] = 0;
               }
            }
         }
         else
            m_pending_count[i] = 0;
         if(MathAbs(m_sls[i] - live_sl) >= 1e-9 || MathAbs(m_tps[i] - live_tp) >= 1e-9)
         {
            // Also covers brokers that never send TRADE_TRANSACTION_POSITION.
            EventClear(ev);
            ev.type = EVT_MODIFY;
            ev.source_pos_id = pos_id;
            ev.symbol = symbol;
            ev.direction = dir;
            ev.sl = live_sl;
            ev.tp = live_tp;
            if(Emit(ev, "reconcile: ")) Update(i, m_volumes[i], live_sl, live_tp);
         }
      }

      // Tracked but gone: the CLOSE was missed. Walk backwards because Untrack() shifts the arrays.
      for(int i = ArraySize(m_pos_ids) - 1; i >= 0; i--)
      {
         if(InLive(live_ids, m_pos_ids[i])) continue;
         m_missing[i]++;
         if(m_missing[i] < RECONCILE_MISSING_SWEEPS) continue;
         // Corroborate with the deal history: absence from the position list alone (e.g. a terminal that
         // is connected but has not re-synced its positions yet) must never flatten the destination.
         // Only when history holds strictly less open volume than the tracker did a close happen that
         // the handler did not see; a consistent tracker (net == tracked) means "keeping".
         double last_out_price = 0;
         double net = HistoryNetVolume(m_pos_ids[i], last_out_price);
         if(net < 0 || net >= m_volumes[i] - 1e-8)
         {
            if(!m_keep_logged[i])
            {
               m_keep_logged[i] = true;
               Print(m_tag, "reconcile: position ", m_pos_ids[i], " not visible but history shows ",
                     (net < 0) ? "no deals" : DoubleToString(net, 2) + " still open", " (tracker ", m_volumes[i], ") - keeping");
            }
            m_missing[i] = RECONCILE_MISSING_SWEEPS - 1;   // re-check on the next sweep
            continue;
         }
         CopyEvent ev;
         EventClear(ev);
         ev.source_pos_id = m_pos_ids[i];
         ev.symbol = m_symbols[i];
         ev.direction = m_dirs[i];
         ev.price = last_out_price;
         if(net < 1e-8)
         {
            ev.type = EVT_CLOSE;
            ev.volume = m_volumes[i];   // tracked remainder (the destination closes the whole mapped position anyway)
            ev.remaining = 0;
            if(Emit(ev, "reconcile: ")) Untrack(m_pos_ids[i]);
         }
         else
         {
            // Partially closed while not visible: report the part history says is gone, keep tracking the rest.
            ev.type = EVT_PARTIAL_CLOSE;
            ev.volume = NormalizeDouble(m_volumes[i] - net, 8);
            ev.remaining = net;
            if(Emit(ev, "reconcile: "))
            {
               Update(i, net, m_sls[i], m_tps[i]);
               m_missing[i] = RECONCILE_MISSING_SWEEPS - 1;   // still absent: re-check next sweep
            }
         }
      }
   }

   void OnTransaction(const MqlTradeTransaction &trans)
   {
      if(trans.type == TRADE_TRANSACTION_DEAL_ADD)
         OnDeal(trans);
      else if(trans.type == TRADE_TRANSACTION_POSITION)
      {
         // trans.position is the position TICKET; our tracker is keyed by position IDENTIFIER
         // (stable across partial closes/ticket changes) - resolve it before dispatching.
         long pos_id = (long)trans.position;
         if(PositionSelectByTicket(trans.position)) pos_id = PositionGetInteger(POSITION_IDENTIFIER);
         OnPositionChange(pos_id, trans.symbol, trans.price_sl, trans.price_tp);
      }
   }
};
#endif
