#ifndef SIGNALBRIDGE_JSON_MQH
#define SIGNALBRIDGE_JSON_MQH
// Flat JSON objects only: {"k":"v","n":1.5}. No nesting, no arrays — the event format never needs them.

// Escapes everything JSON forbids raw inside a string. Control characters matter: an order/position/deal
// comment with a line break (brokers and mobile apps write those) used to make state.json invalid JSON.
string JsonEscape(const string s)
{
   string out = "";
   int n = StringLen(s);
   for(int i = 0; i < n; i++)
   {
      ushort c = StringGetCharacter(s, i);
      if(c == '\\')      out += "\\\\";
      else if(c == '"')  out += "\\\"";
      else if(c == '\n') out += "\\n";
      else if(c == '\r') out += "\\r";
      else if(c == '\t') out += "\\t";
      else if(c < 32)    out += " ";
      else               out += ShortToString(c);
   }
   return out;
}

string JsonUnescape(const string s)
{
   string out = s;
   StringReplace(out, "\\\"", "\"");
   StringReplace(out, "\\\\", "\\");
   return out;
}

// "0.50000000" -> "0.5", "2.00000000" -> "2", "100" -> "100"
string JsonTrimZeros(const string num)
{
   if(StringFind(num, ".") < 0) return num;
   string out = num;
   int len = StringLen(out);
   while(len > 0 && StringGetCharacter(out, len - 1) == '0') { out = StringSubstr(out, 0, len - 1); len--; }
   if(len > 0 && StringGetCharacter(out, len - 1) == '.') out = StringSubstr(out, 0, len - 1);
   return out;
}

class CJsonWriter
{
private:
   string m_buf;
   bool   m_first;
   void   Sep() { if(!m_first) m_buf += ","; m_first = false; }
public:
   CJsonWriter() { Begin(); }
   void   Begin() { m_buf = "{"; m_first = true; }
   void   AddString(const string key, const string value) { Sep(); m_buf += "\"" + key + "\":\"" + JsonEscape(value) + "\""; }
   void   AddLong(const string key, const long value)     { Sep(); m_buf += "\"" + key + "\":" + IntegerToString(value); }
   void   AddDouble(const string key, const double value) { Sep(); m_buf += "\"" + key + "\":" + JsonTrimZeros(DoubleToString(value, 8)); }
   string End() { return m_buf + "}"; }
};

// Finds the raw value for key. Strings are returned unescaped; numbers as their literal text.
bool JsonGetRaw(const string json, const string key, string &raw)
{
   string needle = "\"" + key + "\":";
   int pos = StringFind(json, needle);
   if(pos < 0) return false;
   int len = StringLen(json);
   int i = pos + StringLen(needle);
   while(i < len && StringGetCharacter(json, i) == ' ') i++;
   if(i >= len) return false;
   if(StringGetCharacter(json, i) == '"')
   {
      int start = i + 1;
      int j = start;
      while(j < len)
      {
         ushort c = StringGetCharacter(json, j);
         if(c == '\\') { j += 2; continue; }
         if(c == '"') break;
         j++;
      }
      raw = JsonUnescape(StringSubstr(json, start, j - start));
      return true;
   }
   int end = i;
   while(end < len)
   {
      ushort c = StringGetCharacter(json, end);
      if(c == ',' || c == '}') break;
      end++;
   }
   raw = StringSubstr(json, i, end - i);
   StringTrimLeft(raw);
   StringTrimRight(raw);
   return true;
}

bool JsonGetString(const string json, const string key, string &out) { return JsonGetRaw(json, key, out); }
bool JsonGetDouble(const string json, const string key, double &out)
{
   string raw;
   if(!JsonGetRaw(json, key, raw)) return false;
   out = StringToDouble(raw);
   return true;
}
bool JsonGetLong(const string json, const string key, long &out)
{
   string raw;
   if(!JsonGetRaw(json, key, raw)) return false;
   out = StringToInteger(raw);
   return true;
}
#endif
