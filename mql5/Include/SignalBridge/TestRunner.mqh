#ifndef SIGNALBRIDGE_TESTRUNNER_MQH
#define SIGNALBRIDGE_TESTRUNNER_MQH
// Minimal assertion helpers. Every test prints exactly one "TEST PASS name" or
// "TEST FAIL name: detail" line; TestSummary() prints the line the harness greps for.

int g_tests_passed = 0;
int g_tests_failed = 0;

void TestPass(const string name) { g_tests_passed++; Print("TEST PASS ", name); }
void TestFail(const string name, const string detail) { g_tests_failed++; Print("TEST FAIL ", name, ": ", detail); }

void AssertTrue(const bool cond, const string name)
{
   if(cond) TestPass(name); else TestFail(name, "expected true");
}

void AssertEqStr(const string expected, const string actual, const string name)
{
   if(expected == actual) TestPass(name);
   else TestFail(name, "expected [" + expected + "] got [" + actual + "]");
}

void AssertEqLong(const long expected, const long actual, const string name)
{
   if(expected == actual) TestPass(name);
   else TestFail(name, "expected " + IntegerToString(expected) + " got " + IntegerToString(actual));
}

void AssertEqDbl(const double expected, const double actual, const double eps, const string name)
{
   if(MathAbs(expected - actual) <= eps) TestPass(name);
   else TestFail(name, "expected " + DoubleToString(expected, 8) + " got " + DoubleToString(actual, 8));
}

void TestSummary()
{
   Print("TESTS COMPLETE: ", g_tests_passed, " passed, ", g_tests_failed, " failed");
}
#endif
