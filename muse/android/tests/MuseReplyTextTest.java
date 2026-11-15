package jp.sumi.stopwatch;

public final class MuseReplyTextTest {
    private static void check(String input, String expected) {
        String actual = MuseReplyText.withoutContextStatus(input);
        if (!expected.equals(actual)) throw new AssertionError("Expected " + expected + " but received " + actual);
    }

    public static void main(String[] args) {
        check("おかえり。\n\n[CONTEXT] 12k / 64k\nremaining 52k", "おかえり。");
        check("おかえり。 [CONTEXT]12k", "おかえり。");
        check("おかえり。\n[usage]\nContext size: 12k / 64k", "おかえり。");
        check("おかえり。 [Usage] 20%", "おかえり。");
        check("[usage] 20%", "");
        check("おかえり。\r\n---\r\n📊 **Context size status:** 12k / 64k", "おかえり。");
        check("おかえり。（Context size status: 20%）", "おかえり。");
        check("［Context Status］：20%", "");
        check("What does [CONTEXT] mean?\nContext size status is useful.",
                "What does [CONTEXT] mean?\nContext size status is useful.");
    }
}
