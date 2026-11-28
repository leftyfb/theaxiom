import sqlite3

from theaxiom import kolibri_search, search

CID = "a" * 32
OTHER = "b" * 32


def test_native_search_only_available_selected_channel(tmp_path):
    db = tmp_path / "db.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE content_contentnode (id, channel_id, available, title, description)")
        conn.executemany("INSERT INTO content_contentnode VALUES (?,?,?,?,?)", [
            ("1"*32, CID, 1, "Fractions", "Math"),
            ("2"*32, CID, 1, "Math topic", "Fractions"),
            ("3"*32, CID, 0, "Fractions unavailable", ""),
            ("4"*32, OTHER, 1, "Fractions other channel", ""),
        ])
    route = {"kind": "kolibri", "path": "/kolibri/viewcontent",
             "database": db, "channel_id": CID, "name": "Learning"}
    rows, more = kolibri_search.search_nodes(db, CID, "Fractions", limit=1)
    assert more and rows == [("Fractions", "/kolibri/viewcontent?node_id="+"1"*32, "Math")]
    rows, more = kolibri_search.search_nodes(db, CID, "Fractions", offset=1, limit=1)
    assert not more and rows[0][0] == "Math topic"
    assert kolibri_search.search_nodes(db, CID, "%")[0] == []
    groups = search.search("Fractions", lambda: {}, targets={"learning": (route, search.KOLIBRI_ADAPTER)},
                           allowed=lambda sid, route: True)
    assert len(groups[0]["results"]) == 2
    assert not search.valid_link("/kolibri/viewcontent?node_id=../bad", route)
    assert not search.valid_link("https://elsewhere/kolibri/viewcontent?node_id="+"1"*32, route)
    assert search.search("Fractions", lambda: {}, targets={"learning": (route, search.KOLIBRI_ADAPTER)},
                         allowed=lambda sid, route: False) == []
