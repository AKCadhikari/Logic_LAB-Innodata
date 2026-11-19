from utils.textutil import xml_safe, detect_lang, fix_ligatures, is_rtl, has_nonlatin
from utils.naming import xml_name, pptx_out_name, pair_documents
from pathlib import Path


def test_xml_safe_removes_illegal_characters():
    assert xml_safe("a\ud800b\ufffec\uffffd\x00e\x0bf") == "abcdef"
    assert xml_safe("tab\tnl\n") == "tab\tnl\n"


def test_language_detection():
    assert detect_lang("The quick brown fox is in the garden and it was not at home with the others") == "en"
    assert detect_lang("Der Hund ist nicht in dem Haus und die Katze hat das Essen von der Frau") == "de"
    assert detect_lang("هذا نص عربي طويل بما يكفي للكشف عن اللغة بشكل صحيح") == "ar"
    assert detect_lang("これは日本語の文章であり，言語を判定するために十分な長さがあります") == "ja"
    assert detect_lang("한국어 문장은 언어를 판별하기에 충분히 길게 작성되어 있습니다") == "ko"
    assert detect_lang("xx") == "und"


def test_ligatures_and_scripts():
    assert fix_ligatures("ﬂuctuations oﬃce") == "fluctuations office"
    assert is_rtl("مرحبا بالعالم") and not is_rtl("hello")
    assert has_nonlatin("abc 日本") and not has_nonlatin("abc é")


def test_naming_rules():
    assert xml_name(Path("KSA OG 5065.pdf")) == "KSA OG 5065.xml"
    assert pptx_out_name(Path("amendment3.pptx")) == "amendment_out3.pptx"
    assert pptx_out_name(Path("123_errors.pptx")) == "123_transformed_QA.pptx"
    pairs = pair_documents([Path("input1.pdf"), Path("input2.pdf")], [Path("amendment2.pptx"), Path("amendment1.pptx")])
    assert {(a.name, b.name) for a, b in pairs} == {("input1.pdf", "amendment1.pptx"), ("input2.pdf", "amendment2.pptx")}
    pairs = pair_documents([Path("a.pdf")], [Path("a_errors.pptx")])
    assert pairs == [(Path("a.pdf"), Path("a_errors.pptx"))]
