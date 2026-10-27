#ifndef COPYTRADER_EVENT_MQH
#define COPYTRADER_EVENT_MQH
#include <CopyTrader/Json.mqh>

#define EVT_OPEN          "OPEN"
#define EVT_CLOSE         "CLOSE"
#define EVT_PARTIAL_CLOSE "PARTIAL_CLOSE"
#define EVT_MODIFY        "MODIFY"

// One line of the events file. direction is always the POSITION's direction, even on closes.
struct CopyEvent
{
   string event_id;        // YYYYMMDD-HHMMSS-NNNNNN, monotonic
   string ts;              // "YYYY.MM.DD HH:MM:SS" (source terminal local time)
   string type;            // EVT_*
   long   source_pos_id;   // source position id (join key)
   string symbol;
   string direction;       // "BUY" / "SELL"
   double volume;          // OPEN: opened; CLOSE/PARTIAL_CLOSE: closed; MODIFY: 0
   double remaining;       // PARTIAL_CLOSE: source volume still open; else 0
   double price;           // deal price on the source (informational)
   double sl;              // 0 = none
   double tp;              // 0 = none
   double source_balance;  // source account balance at the moment of the event
   long   login;           // source account login (lets a reader check which account wrote the file)
   long   magic;           // the position's magic number (0 = placed by hand)
   string comment;         // the position's comment (line breaks replaced by spaces)
};

void EventClear(CopyEvent &ev)
{
   ev.event_id = ""; ev.ts = ""; ev.type = ""; ev.source_pos_id = 0;
   ev.symbol = ""; ev.direction = "";
   ev.volume = 0; ev.remaining = 0; ev.price = 0; ev.sl = 0; ev.tp = 0; ev.source_balance = 0;
   ev.login = 0; ev.magic = 0; ev.comment = "";
}

string MakeEventId(const datetime t, const int seq)
{
   MqlDateTime dt;
   TimeToStruct(t, dt);
   return StringFormat("%04d%02d%02d-%02d%02d%02d-%06d", dt.year, dt.mon, dt.day, dt.hour, dt.min, dt.sec, seq);
}

string EventToJson(const CopyEvent &ev)
{
   CJsonWriter w;
   w.AddString("event_id", ev.event_id);
   w.AddString("ts", ev.ts);
   w.AddString("type", ev.type);
   w.AddLong("source_pos_id", ev.source_pos_id);
   w.AddString("symbol", ev.symbol);
   w.AddString("direction", ev.direction);
   w.AddDouble("volume", ev.volume);
   w.AddDouble("remaining", ev.remaining);
   w.AddDouble("price", ev.price);
   w.AddDouble("sl", ev.sl);
   w.AddDouble("tp", ev.tp);
   w.AddDouble("source_balance", ev.source_balance);
   w.AddLong("login", ev.login);
   w.AddLong("magic", ev.magic);
   w.AddString("comment", ev.comment);
   return w.End();
}

// Returns false when event_id, type or source_pos_id is missing; other fields default to ""/0.
bool EventFromJson(const string json, CopyEvent &ev)
{
   EventClear(ev);
   if(!JsonGetString(json, "event_id", ev.event_id)) return false;
   if(!JsonGetString(json, "type", ev.type)) return false;
   if(!JsonGetLong(json, "source_pos_id", ev.source_pos_id)) return false;
   JsonGetString(json, "ts", ev.ts);
   JsonGetString(json, "symbol", ev.symbol);
   JsonGetString(json, "direction", ev.direction);
   JsonGetDouble(json, "volume", ev.volume);
   JsonGetDouble(json, "remaining", ev.remaining);
   JsonGetDouble(json, "price", ev.price);
   JsonGetDouble(json, "sl", ev.sl);
   JsonGetDouble(json, "tp", ev.tp);
   JsonGetDouble(json, "source_balance", ev.source_balance);
   JsonGetLong(json, "login", ev.login);
   JsonGetLong(json, "magic", ev.magic);
   JsonGetString(json, "comment", ev.comment);
   return true;
}

// Seconds between an event's ts ("YYYY.MM.DD HH:MM:SS", source terminal local time) and now.
// Returns 0 when ts cannot be parsed, so an unparsable timestamp never causes a skip.
long EventAgeSec(const string ts, const datetime now)
{
   datetime t = StringToTime(ts);
   if(ts == "" || t <= 0) return 0;
   return (long)(now - t);
}
#endif
