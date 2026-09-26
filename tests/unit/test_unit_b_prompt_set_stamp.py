"""Unit B's prompt set and its stamp move together, or the build fails.

The same guard as Unit A's (register item 146): the templates UNIT_B_TEMPLATES
names are hashed, name and stripped text, and the digest is pinned beside
PROMPT_SET_VERSION. A prompt edit without a bump is a red build.
Both constants are written down by hand, together, when the stamp moves.

Each template is pinned on its own too. A shipped file is never edited -- a
change is a new file -- so a pin moves only when a file is added, and a pin
changed for an existing name is a stamped template edited.
"""

from __future__ import annotations

import hashlib

from dodeal_ai.core import prompting
from dodeal_ai.units.call_intelligence.prompts import (
    PROMPT_SET_VERSION,
    UNIT_B_TEMPLATES,
)

PROMPT_SET = "unit_b_prompts_v18"
PROMPT_SET_DIGEST = "418dce82a8dd80b23111e8a8887c2192808fe8e2990dd13634dc13883cede790"

_HOW_TO_UPDATE = (
    "A Unit B prompt template changed. Bump PROMPT_SET_VERSION in "
    "units/call_intelligence/prompts.py and update PROMPT_SET and "
    "PROMPT_SET_DIGEST here, in the same commit."
)


def _digest() -> str:
    """sha256 over every template name and its stripped text, in tuple order,
    read from the shipped directory and never a DODEAL_PROMPTS_DIR."""
    digest = hashlib.sha256()
    for name in UNIT_B_TEMPLATES:
        text = (prompting._DEFAULT_PROMPTS_DIR / name).read_text(encoding="utf-8")
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(text.strip().encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def test_the_unit_b_prompt_set_digest_matches_its_stamp() -> None:
    """The shipped templates hash to the digest pinned for this stamp."""
    assert PROMPT_SET_VERSION == PROMPT_SET, _HOW_TO_UPDATE
    assert _digest() == PROMPT_SET_DIGEST, _HOW_TO_UPDATE


# Each shipped template's sha256, over its stripped text.
TEMPLATE_DIGESTS = {
    "call_intelligence/roles_v1.txt": (
        "eee1172cb8ff1130a9c6cc9ea646bc3a09e72b4479f4a24079a4ae1977701676"
    ),
    "call_intelligence/extract_v2.txt": (
        "1d7fa2d4cd37248cbfe7f76b6f28932ae665a9ce23bf6154e77c85ebd1273190"
    ),
    "call_intelligence/prose_v1.txt": (
        "081f1bceb3b70232525416d39350f35bdd2963d59488c5f7a9dadf11df23efe8"
    ),
    "call_intelligence/objections_v1.txt": (
        "fc6bf0a517c76356f72676f5fbab4d95688b115939237b5156da3d61e7f1f3c0"
    ),
    "call_intelligence/score_v1.txt": (
        "1313ff49da49c99ad8a710dc2975954b4c4c15851a24c4521c49060bce0df403"
    ),
    "call_intelligence/escalations_v1.txt": (
        "3b993bfe33c0b6f8a87529dd61f3b28613a8cfa1c8e1222021ce577ed6029df7"
    ),
    "call_intelligence/coaching_v1.txt": (
        "2ce19501575ebd005a99c0e8957ceb395bc26eb665907a226c3618c7b0b76898"
    ),
    "call_intelligence/extras_v1.txt": (
        "85eee2b53e62d8d842947369a30df0e8dce8255789d7015a7ae5a14a2ae55e00"
    ),
    "call_intelligence/extras_v2.txt": (
        "a9db4f99aefb2dbc3420b24084b659b1c5fe2a01a5d1681a7ceb23f6d986c011"
    ),
    "call_intelligence/translate_v1.txt": (
        "16bf7a049b9b10ad1f29e904276d9dce1795c3ee8c4d2a0c74c068f79fcae5e4"
    ),
    "call_intelligence/reprompt_tail_v1.txt": (
        "a342aec7b7a3ad284e2d64c636729b935bc6dc87e749408a5009befef3445a66"
    ),
    "call_intelligence/roles_v2.txt": (
        "caaea610c04509315f485e69c48edf0db1810acf99a99cb151332da27dff7dad"
    ),
    "call_intelligence/extract_v3.txt": (
        "08650c7bfd444a27aa973770e7b766197e30a62ffc84566fa628e02efb0e1071"
    ),
    "call_intelligence/objections_v2.txt": (
        "6e06a6b5617d608b4ae599b0832dff7b7ed71a57b087aea1529ec1c7d925f4fc"
    ),
    "call_intelligence/score_v2.txt": (
        "2c67882d82e0ad341308a778c3ba16b7bfe5f5af19be541863a6a1e4fd5d94bb"
    ),
    "call_intelligence/escalations_v2.txt": (
        "4230d6ca499def22e29aa7c2ba380139243f8d85aba176cdfe68a6d141b61744"
    ),
    "call_intelligence/coaching_v2.txt": (
        "564824ec9598d5939981342f85e36a89fb502e64989eb679a96329abf8c0e0cf"
    ),
    "call_intelligence/extras_v3.txt": (
        "69f8c6a9c0805817ea1eee89279be86d886dc38c562d0d7e02673fd1b9cd636e"
    ),
    "call_intelligence/extras_v4.txt": (
        "553c5e36b3f13811b121255937a0cb43fe30c020795ab74f209759fd0cfc6e48"
    ),
    "call_intelligence/roles_v3.txt": (
        "b1708bb1f390cb1d06de0583843a1a1e9f6bfdec575ab8e87b5e72885737f0b0"
    ),
    "call_intelligence/extras_v5.txt": (
        "9d4988e38a2580838d2af3d1a4ed500b978512d88370fb66335ccc935744daa0"
    ),
    "call_intelligence/whatsapp_v1.txt": (
        "fc5e4d9b83459df0765a5fc505d7fc3714dcfd1cc823268a7f2803bdcf6f805e"
    ),
    "call_intelligence/reprompt_tail_quote_length_v1.txt": (
        "a734e2729598f78474618be27844d944063fe66a43b17675258e3e23c63b16b4"
    ),
    "call_intelligence/reprompt_tail_quote_exact_v1.txt": (
        "8bab7e18bf98a2bb7728c18006b5cb500b6d2d34ec424f47fb50e22ce52ace87"
    ),
    "call_intelligence/extract_v1.txt": (
        "7ccf78ffa14164c08f5f21b90b73864354559cf9f0d99e1a7e8880ea30ae839a"
    ),
    "call_intelligence/extract_v4.txt": (
        "3c1611686a42cd14329214712c98f8f7244639f0be2cf7d24f68a01d33b3ff2c"
    ),
    "call_intelligence/extras_v6.txt": (
        "c0249a6b5143a2be7c59a3fc0b414da2fe0db221938b6a1b4b81ce06ab0c6e3d"
    ),
    "call_intelligence/escalations_v3.txt": (
        "0b5381db2af967deeb0a419050ac53c0f5f1ead8114dd02a2867115d322ddbbd"
    ),
    "call_intelligence/reprompt_tail_whatsapp_v1.txt": (
        "f8e13525109bc6bbd15c334d91d2be41ef5e353661534d561319e52a294c1dac"
    ),
    "call_intelligence/extract_v5.txt": (
        "3d48dcc519d5aadf60c87e6229e4dd72ee818ada8be44fdac312319a7fb967e5"
    ),
    "call_intelligence/prose_v2.txt": (
        "bfc562e70fd548dacefb9300f9cd85ba88cb58c2cc27658bf14c6a63fe80fc9c"
    ),
    "call_intelligence/coaching_v3.txt": (
        "1b0920399e47cde5f7d98dd2bd2b23d9f3863bc03a70132f4a0a31b2637d34b9"
    ),
    "call_intelligence/extras_v7.txt": (
        "436916459b3c24238ae86819d48d5d0f21daadd720fd9e69112c67d509d94ce4"
    ),
    "call_intelligence/translate_v2.txt": (
        "66848e3e57eb62107ca80caa889ede23e17f94b9ede45618180145ca9c286e60"
    ),
    "call_intelligence/whatsapp_v2.txt": (
        "0e77762f1cb499df8941620c9869e906a93862b542413bcb57a893780d875f03"
    ),
    "call_intelligence/extras_v8.txt": (
        "b670c3db4c2f13c87777378d2109eca7ed8e1495b4e144bec97ad8b0f57e7e4d"
    ),
    "call_intelligence/whatsapp_v3.txt": (
        "75b8eb8ab388f75e4b0d6ec9303eea8ec4ebb62b6a4b075fd2b600cec4053c3c"
    ),
    "call_intelligence/roles_v4.txt": (
        "4e37eb8108dbbc62cfcc19e56e09a154057cb12c82c460c09a3023c0ac6b7182"
    ),
    "call_intelligence/extract_v6.txt": (
        "a18cef47f9dfbe8fdf5591a08b101ae48557706d514b9d271843070d8779b3f7"
    ),
    "call_intelligence/extract_v7.txt": (
        "d6bf706fdee4c998634a13389e7779ccd9e34166fe1b9474b783171bbbf16ebe"
    ),
    "call_intelligence/extract_v8.txt": (
        "736f37fca4f25c81b37f1ad5efe906efd03e804ba3afca7b43bc74b16f30453e"
    ),
    "call_intelligence/coaching_v4.txt": (
        "cf9a5d49035d322d18a00f568ae33b541894b8dff857d0f05c657db32d360b73"
    ),
}


def test_no_shipped_template_is_ever_edited() -> None:
    """Every template in the set is pinned, and hashes to its own pin."""
    shipped = {
        name: hashlib.sha256(
            (prompting._DEFAULT_PROMPTS_DIR / name)
            .read_text(encoding="utf-8")
            .strip()
            .encode("utf-8")
        ).hexdigest()
        for name in UNIT_B_TEMPLATES
    }
    assert shipped == TEMPLATE_DIGESTS
