package jp.sumi.stopwatch;

import java.util.regex.Pattern;

final class MuseReplyText {
    private static final String STATUS = "context[\\h_-]+(?:size[\\h_-]+status|(?:size|window)[\\h_-]+usage|window[\\h_-]+status)";
    private static final String TAG = "[\\[［]\\h*(?:usage|context(?:[\\h_-]+(?:size(?:[\\h_-]+status)?|status))?)\\h*[\\]］]";
    private static final Pattern FOOTER = Pattern.compile("(?im)(?:"
            + "^[\\p{Punct}\\p{So}\\ufe0f\\h]*" + TAG + "|(?<=。|！|？|\\.|!|\\?)\\h*" + TAG + "|"
            + "(?:^[\\p{Punct}\\p{So}\\ufe0f\\h]*|"
            + "(?<=。|！|？)\\h*[\\p{So}\\ufe0f\\h]*|\\h*[（(\\[【|│—]\\h*[\\x60*_]*)"
            + STATUS + "[\\x60*_\\])）】]*(?:\\h*[:：=|\\-–—]|\\h+[0-9]|\\h*$))");
    private static final Pattern RULE = Pattern.compile("(?:^|\\R)\\h*(?:-{3,}|_{3,}|\\*{3,}|─{3,})\\h*$");

    static String withoutContextStatus(String text) {
        var marker = FOOTER.matcher(text);
        if (!marker.find()) return text;
        String body = text.substring(0, marker.start()).stripTrailing();
        return RULE.matcher(body).replaceFirst("").strip();
    }

    private MuseReplyText() {}
}
