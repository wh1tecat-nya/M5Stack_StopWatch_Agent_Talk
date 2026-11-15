package jp.sumi.stopwatch;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.regex.Pattern;

final class IrodoriModels {
    record Model(String name, long bytes, String sha256, boolean anime) {}
    record Part<T>(String name, T source) {}
    record Selection<T>(Model model, List<Part<T>> parts) {}

    static final Model MF = new Model("Irodori-TTS-866M-MF-v4.1-F16.gguf", 1920321792L,
            "e237dc3eefe469b8bba87872beacf12fb7fe00dbdf85767edca8ec04ab910d13", false);
    private static final List<Model> MODELS = List.of(MF,
            anime("F16", 1905904000L, "8fb631722532e028cb6afa567993f7d03980b3cede4a5a55afc9be9e7aeb5a46"),
            anime("Q8_0", 1199474592L, "dfc6ca8c46e890156c95e5ab042e72ff989e42ce3f3250a06b299b4a21807d2a"),
            anime("Q6_K", 1034979552L, "c8508a8e679d7e54254df6e852df60ccbdd1d3272f058371da1694cf3eedd7c6"),
            anime("Q4_K", 859871136L, "f712c33344b3d89f286ee0a0422d59fb9993bb9cf496544572aa6e50c0b2ed87"),
            anime("Q3_K", 769664096L, "02fd385f5dc11a26e4c0fdf63e994ab3cc20eb1ef515cd5c758ec9cc7f5153ce"),
            anime("Q2_K", 700682272L, "ccc80b36ffb55233a47844957d2c91891bfbdb7a3f65b2dbb36267ec979b4f76"));
    private static final Pattern SPLIT = Pattern.compile("(.+[.]gguf)[.]part([0-9]{2,3})");

    private static Model anime(String quantization, long bytes, String sha256) {
        return new Model("Irodori-TTS-859M-Anime-v4.1-" + quantization + ".gguf", bytes, sha256, true);
    }

    static Model bySize(long bytes) {
        return MODELS.stream().filter(model -> model.bytes() == bytes).findFirst().orElse(null);
    }

    static long maximumSize() { return MF.bytes(); }

    static <T> Selection<T> select(List<Part<T>> selected) throws NetworkTask.Failure {
        if (selected.isEmpty() || selected.size() > 99) throw new NetworkTask.Failure("モデルを1組選んでください");
        String name = selected.get(0).name();
        var first = SPLIT.matcher(name);
        boolean split = first.matches();
        String base = split ? first.group(1) : name;
        Model model = MODELS.stream().filter(item -> item.name().equals(base)).findFirst().orElse(null);
        if (!split) {
            if (selected.size() != 1) throw new NetworkTask.Failure("単体GGUFは1ファイル選んでください");
            return new Selection<>(model, selected);
        }
        if (model == null) throw new NetworkTask.Failure("対応するIrodoriモデルの分割ファイルを選んでください");
        var numbered = new ArrayList<NumberedPart<T>>();
        for (Part<T> part : selected) {
            var match = SPLIT.matcher(part.name());
            if (!match.matches() || !base.equals(match.group(1)))
                throw new NetworkTask.Failure("同じモデルの分割ファイルだけを選んでください");
            numbered.add(new NumberedPart<>(Integer.parseInt(match.group(2)), part));
        }
        numbered.sort(Comparator.comparingInt(NumberedPart::index));
        var ordered = new ArrayList<Part<T>>(numbered.size());
        for (int i = 0; i < numbered.size(); i++) {
            if (numbered.get(i).index() != i + 1)
                throw new NetworkTask.Failure("part01から最後まで、分割ファイルをすべて選んでください");
            ordered.add(numbered.get(i).part());
        }
        return new Selection<>(model, ordered);
    }

    private record NumberedPart<T>(int index, Part<T> part) {}

    private IrodoriModels() {}
}
