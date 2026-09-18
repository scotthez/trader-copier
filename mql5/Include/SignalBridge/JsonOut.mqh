#ifndef SIGNALBRIDGE_JSONOUT_MQH
#define SIGNALBRIDGE_JSONOUT_MQH
#include <SignalBridge/Json.mqh>
// Streaming writer for nested JSON (objects/arrays). Tracks whether a comma is needed per level.

class CJsonOut
{
private:
   string m_buf;
   bool   m_need_comma[];   // per open container
   int    m_depth;
   bool   m_after_key;      // a value (or container) is about to follow a Key()

   void Sep()
   {
      if(m_depth == 0) return;
      if(m_need_comma[m_depth - 1]) m_buf += ",";
      m_need_comma[m_depth - 1] = true;
   }
   // Called before every value/container: after a key no separator is needed (the key wrote it).
   void Value()
   {
      if(m_after_key) { m_after_key = false; return; }
      Sep();
   }
   void Push() { m_depth++; ArrayResize(m_need_comma, m_depth); m_need_comma[m_depth - 1] = false; }
   void Pop()  { if(m_depth > 0) { m_depth--; ArrayResize(m_need_comma, m_depth); } }

public:
   CJsonOut() { m_buf = ""; m_depth = 0; m_after_key = false; }
   void BeginObject() { Value(); m_buf += "{"; Push(); }
   void EndObject()   { Pop(); m_buf += "}"; }
   void BeginArray()  { Value(); m_buf += "["; Push(); }
   void EndArray()    { Pop(); m_buf += "]"; }
   void Key(const string key) { Sep(); m_buf += "\"" + JsonEscape(key) + "\":"; m_after_key = true; }
   void Str(const string v)   { Value(); m_buf += "\"" + JsonEscape(v) + "\""; }
   void Num(const double v)   { Value(); m_buf += JsonTrimZeros(DoubleToString(v, 8)); }
   void Int(const long v)     { Value(); m_buf += IntegerToString(v); }
   void Bool(const bool v)    { Value(); m_buf += (v ? "true" : "false"); }
   string Text() { return m_buf; }
};
#endif
