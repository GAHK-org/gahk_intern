"""The søndagsrengøring areas and their task lists, as `load_cleaning_areas` seeds them.

Transcribed from the wiki page "Rengøring" (the sections before Hovedrengøring) by way of the
prototype inspector app that was used for a real round on 2026-11-01. A task starting with "*" only
applies in (*)-uger. Once seeded, the lists are Inspektionen's to edit in the Django admin; this
module is only the starting point, so it is never re-applied over existing areas without --reset.
"""

# (name, description, import aliases, [(zone, [(group, [task, ...]), ...]), ...])
AREAS: list[tuple[str, str, list[str], list[tuple[str, list[tuple[str, list[str]]]]]]] = [
    (
        "Hall og Vindfang",
        "Inkl. stuekøkken og pallen · deles af 3 alumner",
        ["Hallen, Vindfang, Køkken"],
        [
            (
                "Hall og Vindfang",
                [
                    ("Planter", ["Planterne vandet"]),
                    (
                        "Diverse flader",
                        [
                            "Vindueskarme aftørret",
                            "Toppen af bogreolerne aftørret",
                            "Sofabordet aftørret",
                            "Aviser samlet og smidt i avisskraldespanden i gården",
                            "*Alle radiatorer og paneler aftørret",
                        ],
                    ),
                    (
                        "Service og flasker",
                        [
                            "Kollegiets service båret ned i køkkenet, opvask taget",
                            "Flasker og dåser båret ned i ølkælderen eller baggården",
                        ],
                    ),
                    (
                        "Gulv",
                        ["Hall, løberen i hallen og vindfang støvsuget", "Hall og trappe i vindfang vasket"],
                    ),
                    ("Klaver", ["Tangenter aftørret med TØR klud"]),
                ],
            ),
            (
                "Stuekøkkenet",
                [
                    (
                        "Skraldespand",
                        [
                            "Alle skraldespande tømt, affald båret ned, ny pose i",
                            "Skraldespande aftørret, skrubbet efter behov",
                        ],
                    ),
                    ("Køleskab", ["Køleskab tømt for gammel mad, aftørret indeni efter behov"]),
                    ("Planter", ["Planterne vandet"]),
                    ("Mikrobølgeovn", ["Mikrobølgeovn aftørret indvendigt"]),
                    (
                        "Køkkenbord, vask og flader",
                        [
                            "Borde, væg og panel ved køkkenbord aftørret",
                            "Vask rengjort med skurecreme",
                            "Lamper og vindueskarm aftørret",
                            "*Radiator, paneler og køkkendør aftørret",
                        ],
                    ),
                    (
                        "Service og flasker",
                        [
                            "Kollegiets service båret ned i køkkenet, opvask taget",
                            "Flasker båret ned i ølkælderen eller baggården",
                        ],
                    ),
                    (
                        "Rengøringsartikler",
                        [
                            "Gamle håndklæder/viskestykker/karklude/svampe sendt til viskevask",
                            "Rene håndklæder/viskestykker/karklude/svampe hentet",
                            "Rengøringsmidler, håndopvask og sorte sække fyldt op",
                        ],
                    ),
                    ("Gulv", ["Gulvet støvsuget og vasket"]),
                ],
            ),
            (
                "Pallen",
                [
                    (
                        "Service og flasker",
                        [
                            "Kollegiets service båret ned, opvask taget",
                            "Flasker båret ned i ølkælderen eller baggården",
                        ],
                    ),
                    ("Diverse flader", ["Borde og hynder aftørret"]),
                ],
            ),
        ],
    ),
    (
        "Sal, stuen",
        "Deles af 3 alumner",
        ["Stuen, Gang, Toilet"],
        [
            (
                "Gangen",
                [
                    (
                        "Gulvet",
                        [
                            "Gang støvsuget, radiatorskjuler aftørret og ryddet",
                            "*Gangspejlet pudset og paneler aftørret",
                        ],
                    ),
                ],
            ),
            (
                "Toilet og bad",
                [
                    (
                        "Affald",
                        [
                            "Toiletspande tømt og aftørret efter behov",
                            "Skraldespand tømt, affald båret ned i skralderummet",
                        ],
                    ),
                    (
                        "Toiletter",
                        [
                            "Toiletter rengjort med toiletrens, ind- og udvendigt",
                            "*Døre, dørkarm og radiator aftørret",
                            "Spejl (med spejlrens), håndtag og andre flader aftørret",
                        ],
                    ),
                    (
                        "Håndvask",
                        [
                            "Håndvask rengjort med skurecreme og eftertørret",
                            "Fliser ved vasken aftørret",
                            "Håndsæbe fyldt op",
                            "Håndsprit fyldt op",
                        ],
                    ),
                    (
                        "Baderum",
                        [
                            "Fliser aftørret med kalkfjerner, gulv skrubbet",
                            "Rist og vandlås tømt for hår, området omkring skrubbet",
                            "Armatur aftørret med kalkfjerner",
                        ],
                    ),
                    (
                        "Rengøringsartikler",
                        [
                            "Gamle håndklæder sendt til viskevask",
                            "Rene håndklæder hentet",
                            "Rengøringsmidler, sorte sække og toiletpapir til en hel uge",
                            "Hygiejneprodukter fyldt op",
                        ],
                    ),
                ],
            ),
        ],
    ),
    (
        "1. sal",
        "Deles af 4 alumner",
        [],
        [
            (
                "Gang",
                [
                    (
                        "Gulvet",
                        [
                            "Gang støvsuget, radiatorskjuler ryddet og aftørret",
                            "Støvsugerpose skiftet hvis fyldt",
                            "*Paneler og branddøre aftørret",
                            "Toilet/bad-gulv støvsuget, også bag dørene",
                            "Toiletgulv vasket",
                            "Køkken støvsuget (vasket hvis nødvendigt)",
                        ],
                    ),
                ],
            ),
            (
                "Køkkenet",
                [
                    (
                        "Skraldespand",
                        [
                            "Alle skraldespande tømt, affald båret ned, ny pose i",
                            "Skraldespande aftørret, skrubbet efter behov",
                        ],
                    ),
                    ("Køleskab", ["Køleskab tømt for gammel mad, aftørret indeni efter behov"]),
                    ("Planter", ["Planterne vandet"]),
                    ("Mikrobølgeovn", ["Mikrobølgeovn aftørret indvendigt"]),
                    (
                        "Køkkenbord, vask og flader",
                        [
                            "Borde, væg og panel ved køkkenbord aftørret",
                            "Vask rengjort med skurecreme",
                            "Lamper og vindueskarm aftørret",
                            "*Radiator, paneler og køkkendør aftørret",
                        ],
                    ),
                    (
                        "Service og flasker",
                        [
                            "Kollegiets service båret ned i køkkenet, opvask taget",
                            "Flasker båret ned i ølkælderen eller baggården",
                        ],
                    ),
                    (
                        "Rengøringsartikler",
                        [
                            "Gamle håndklæder/viskestykker/karklude/svampe sendt til viskevask",
                            "Rene håndklæder/viskestykker/karklude/svampe hentet",
                            "Rengøringsmidler, håndopvask og sorte sække fyldt op",
                        ],
                    ),
                ],
            ),
            (
                "Toilet og bad",
                [
                    (
                        "Affald",
                        [
                            "Toiletspande tømt og aftørret efter behov",
                            "Skraldespand tømt, affald båret ned i skralderummet",
                        ],
                    ),
                    (
                        "Toiletter",
                        [
                            "Toiletter rengjort med toiletrens, ind- og udvendigt",
                            "*Døre, dørkarm og radiator aftørret",
                            "Spejl (med spejlrens), håndtag og andre flader aftørret",
                        ],
                    ),
                    (
                        "Håndvask",
                        [
                            "Håndvask rengjort med skurecreme og eftertørret",
                            "Fliser ved vasken aftørret",
                            "Håndsæbe fyldt op",
                            "Håndsprit fyldt op",
                        ],
                    ),
                    (
                        "Baderum",
                        [
                            "Fliser aftørret med kalkfjerner, gulv skrubbet",
                            "Rist og vandlås tømt for hår, området omkring skrubbet",
                            "Armatur aftørret med kalkfjerner",
                        ],
                    ),
                    (
                        "Rengøringsartikler",
                        [
                            "Gamle håndklæder sendt til viskevask",
                            "Rene håndklæder hentet",
                            "Rengøringsmidler, sorte sække og toiletpapir til en hel uge",
                            "Hygiejneprodukter fyldt op",
                        ],
                    ),
                ],
            ),
        ],
    ),
    (
        "2. sal",
        "Deles af 4 alumner",
        [],
        [
            (
                "Gang",
                [
                    (
                        "Gulvet",
                        [
                            "Gang støvsuget, radiatorskjuler ryddet og aftørret",
                            "Støvsugerpose skiftet hvis fyldt",
                            "*Paneler og branddøre aftørret",
                            "Toilet/bad-gulv støvsuget, også bag dørene",
                            "Toiletgulv vasket",
                            "Køkken støvsuget (vasket hvis nødvendigt)",
                        ],
                    ),
                ],
            ),
            (
                "Køkkenet",
                [
                    (
                        "Skraldespand",
                        [
                            "Alle skraldespande tømt, affald båret ned, ny pose i",
                            "Skraldespande aftørret, skrubbet efter behov",
                        ],
                    ),
                    ("Køleskab", ["Køleskab tømt for gammel mad, aftørret indeni efter behov"]),
                    ("Planter", ["Planterne vandet"]),
                    ("Mikrobølgeovn", ["Mikrobølgeovn aftørret indvendigt"]),
                    (
                        "Køkkenbord, vask og flader",
                        [
                            "Borde, væg og panel ved køkkenbord aftørret",
                            "Vask rengjort med skurecreme",
                            "Lamper og vindueskarm aftørret",
                            "*Radiator, paneler og køkkendør aftørret",
                        ],
                    ),
                    (
                        "Service og flasker",
                        [
                            "Kollegiets service båret ned i køkkenet, opvask taget",
                            "Flasker båret ned i ølkælderen eller baggården",
                        ],
                    ),
                    (
                        "Rengøringsartikler",
                        [
                            "Gamle håndklæder/viskestykker/karklude/svampe sendt til viskevask",
                            "Rene håndklæder/viskestykker/karklude/svampe hentet",
                            "Rengøringsmidler, håndopvask og sorte sække fyldt op",
                        ],
                    ),
                ],
            ),
            (
                "Toilet og bad",
                [
                    (
                        "Affald",
                        [
                            "Toiletspande tømt og aftørret efter behov",
                            "Skraldespand tømt, affald båret ned i skralderummet",
                        ],
                    ),
                    (
                        "Toiletter",
                        [
                            "Toiletter rengjort med toiletrens, ind- og udvendigt",
                            "*Døre, dørkarm og radiator aftørret",
                            "Spejl (med spejlrens), håndtag og andre flader aftørret",
                        ],
                    ),
                    (
                        "Håndvask",
                        [
                            "Håndvask rengjort med skurecreme og eftertørret",
                            "Fliser ved vasken aftørret",
                            "Håndsæbe fyldt op",
                            "Håndsprit fyldt op",
                        ],
                    ),
                    (
                        "Baderum",
                        [
                            "Fliser aftørret med kalkfjerner, gulv skrubbet",
                            "Rist og vandlås tømt for hår, området omkring skrubbet",
                            "Armatur aftørret med kalkfjerner",
                        ],
                    ),
                    (
                        "Rengøringsartikler",
                        [
                            "Gamle håndklæder sendt til viskevask",
                            "Rene håndklæder hentet",
                            "Rengøringsmidler, sorte sække og toiletpapir til en hel uge",
                            "Hygiejneprodukter fyldt op",
                        ],
                    ),
                ],
            ),
        ],
    ),
    (
        "3. sal",
        "Deles af 4 alumner",
        [],
        [
            (
                "Gang",
                [
                    (
                        "Gulvet",
                        [
                            "Gang støvsuget, radiatorskjuler ryddet og aftørret",
                            "Støvsugerpose skiftet hvis fyldt",
                            "*Paneler og branddøre aftørret",
                            "Toilet/bad-gulv støvsuget, også bag dørene",
                            "Toiletgulv vasket",
                            "Køkken støvsuget (vasket hvis nødvendigt)",
                        ],
                    ),
                ],
            ),
            (
                "Køkkenet",
                [
                    (
                        "Skraldespand",
                        [
                            "Alle skraldespande tømt, affald båret ned, ny pose i",
                            "Skraldespande aftørret, skrubbet efter behov",
                        ],
                    ),
                    ("Køleskab", ["Køleskab tømt for gammel mad, aftørret indeni efter behov"]),
                    ("Planter", ["Planterne vandet"]),
                    ("Mikrobølgeovn", ["Mikrobølgeovn aftørret indvendigt"]),
                    (
                        "Køkkenbord, vask og flader",
                        [
                            "Borde, væg og panel ved køkkenbord aftørret",
                            "Vask rengjort med skurecreme",
                            "Lamper og vindueskarm aftørret",
                            "*Radiator, paneler og køkkendør aftørret",
                        ],
                    ),
                    (
                        "Service og flasker",
                        [
                            "Kollegiets service båret ned i køkkenet, opvask taget",
                            "Flasker båret ned i ølkælderen eller baggården",
                        ],
                    ),
                    (
                        "Rengøringsartikler",
                        [
                            "Gamle håndklæder/viskestykker/karklude/svampe sendt til viskevask",
                            "Rene håndklæder/viskestykker/karklude/svampe hentet",
                            "Rengøringsmidler, håndopvask og sorte sække fyldt op",
                        ],
                    ),
                ],
            ),
            (
                "Toilet og bad",
                [
                    (
                        "Affald",
                        [
                            "Toiletspande tømt og aftørret efter behov",
                            "Skraldespand tømt, affald båret ned i skralderummet",
                        ],
                    ),
                    (
                        "Toiletter",
                        [
                            "Toiletter rengjort med toiletrens, ind- og udvendigt",
                            "*Døre, dørkarm og radiator aftørret",
                            "Spejl (med spejlrens), håndtag og andre flader aftørret",
                        ],
                    ),
                    (
                        "Håndvask",
                        [
                            "Håndvask rengjort med skurecreme og eftertørret",
                            "Fliser ved vasken aftørret",
                            "Håndsæbe fyldt op",
                            "Håndsprit fyldt op",
                        ],
                    ),
                    (
                        "Baderum",
                        [
                            "Fliser aftørret med kalkfjerner, gulv skrubbet",
                            "Rist og vandlås tømt for hår, området omkring skrubbet",
                            "Armatur aftørret med kalkfjerner",
                        ],
                    ),
                    (
                        "Rengøringsartikler",
                        [
                            "Gamle håndklæder sendt til viskevask",
                            "Rene håndklæder hentet",
                            "Rengøringsmidler, sorte sække og toiletpapir til en hel uge",
                            "Hygiejneprodukter fyldt op",
                        ],
                    ),
                ],
            ),
        ],
    ),
    (
        "4. sal og Lounge",
        "Deles af 4 alumner",
        ["4. sal"],
        [
            (
                "Gang",
                [
                    (
                        "Gulvet",
                        [
                            "Gang støvsuget, radiatorskjuler ryddet",
                            "Gulvet i garderoben støvsuget",
                            "Støvsugerpose skiftet hvis fyldt",
                            "*Paneler og branddøre aftørret",
                            "Toilet/bad-gulv støvsuget, også bag dørene",
                            "Toiletgulv vasket",
                            "Køkken støvsuget (vasket hvis nødvendigt)",
                        ],
                    ),
                ],
            ),
            (
                "Køkkenet",
                [
                    (
                        "Skraldespand",
                        [
                            "Alle skraldespande tømt, affald båret ned, ny pose i",
                            "Skraldespande aftørret, skrubbet efter behov",
                        ],
                    ),
                    ("Køleskab", ["Køleskab tømt for gammel mad, aftørret indeni efter behov"]),
                    ("Planter", ["Planterne vandet"]),
                    ("Mikrobølgeovn", ["Mikrobølgeovn aftørret indvendigt"]),
                    (
                        "Køkkenbord, vask og flader",
                        [
                            "Borde, væg og panel ved køkkenbord aftørret",
                            "Vask rengjort med skurecreme",
                            "Lamper og vindueskarm aftørret",
                            "*Radiator, paneler og køkkendør aftørret",
                        ],
                    ),
                    (
                        "Service og flasker",
                        [
                            "Kollegiets service båret ned i køkkenet, opvask taget",
                            "Flasker båret ned i ølkælderen eller baggården",
                        ],
                    ),
                    (
                        "Rengøringsartikler",
                        [
                            "Gamle håndklæder/viskestykker/karklude/svampe sendt til viskevask",
                            "Rene håndklæder/viskestykker/karklude/svampe hentet",
                            "Rengøringsmidler, håndopvask og sorte sække fyldt op",
                        ],
                    ),
                ],
            ),
            (
                "Toilet og bad",
                [
                    (
                        "Affald",
                        [
                            "Toiletspande tømt og aftørret efter behov",
                            "Skraldespand tømt, affald båret ned i skralderummet",
                        ],
                    ),
                    (
                        "Toiletter",
                        [
                            "Toiletter rengjort med toiletrens, ind- og udvendigt",
                            "*Døre, dørkarm og radiator aftørret",
                            "Spejl (med spejlrens), håndtag og andre flader aftørret",
                        ],
                    ),
                    (
                        "Håndvask",
                        [
                            "Håndvask rengjort med skurecreme og eftertørret",
                            "Fliser ved vasken aftørret",
                            "Håndsæbe fyldt op",
                            "Håndsprit fyldt op",
                        ],
                    ),
                    (
                        "Baderum",
                        [
                            "Fliser aftørret med kalkfjerner, gulv skrubbet",
                            "Rist og vandlås tømt for hår, området omkring skrubbet",
                            "Armatur aftørret med kalkfjerner",
                        ],
                    ),
                    (
                        "Rengøringsartikler",
                        [
                            "Gamle håndklæder sendt til viskevask",
                            "Rene håndklæder hentet",
                            "Rengøringsmidler, sorte sække og toiletpapir til en hel uge",
                            "Hygiejneprodukter fyldt op",
                        ],
                    ),
                ],
            ),
            (
                "Lounge",
                [
                    (
                        "Lounge",
                        [
                            "Gulvet støvsuget og vasket",
                            "Sofa støvsuget",
                            "Borde, hylder og anlæg aftørret",
                            "Efterladt service båret ned i KEN",
                            "*Skabslåger aftørret",
                        ],
                    ),
                ],
            ),
        ],
    ),
    (
        "Læsesalen",
        "Inkl. trapper og arkiv · deles af 3 alumner",
        ["Læsesal"],
        [
            (
                "Læsesalen",
                [
                    (
                        "Salen",
                        [
                            "Gulvet støvsuget og vasket",
                            "Hylderne aftørret",
                            "Fatboys og stole aftørret med fugtig klud",
                            "Vindueskarme aftørret",
                            "*Døre, radiatorer, paneler og rør ved Tyskernes Sovesal aftørret",
                        ],
                    ),
                    ("Borde", ["Borde vasket", "Computerskærm og tastatur aftørret"]),
                    (
                        "Service og flasker",
                        [
                            "Kollegiets service båret ned, opvask taget",
                            "Flasker og dåser båret ned i ølkælderen eller baggården",
                        ],
                    ),
                    (
                        "Skraldespande",
                        [
                            "Skraldespande tømt, affald båret ned, nye poser i",
                            "Skraldespande aftørret, skrubbet efter behov",
                        ],
                    ),
                    ("Planter", ["Planterne vandet"]),
                ],
            ),
            (
                "Trapper",
                [
                    ("Trapper", ["Trapperne støvsuget og vasket", "Gelændere aftørret", "*Paneler aftørret"]),
                ],
            ),
            (
                "Arkiv",
                [
                    ("Arkiv", ["*Gulv støvsuget og vasket, reoler aftørret"]),
                ],
            ),
        ],
    ),
    (
        "Varme trappe",
        "Inkl. musiklokalet · deles af 3 alumner",
        ["Varme trappe + Musik"],
        [
            (
                "Trappe",
                [
                    (
                        "Trappe",
                        [
                            "Trappen og gulvet foran postkasserne fejet og vasket",
                            "Vindueskarme aftørret",
                            "Gelænderet aftørret",
                            "*Vinduer i gangdørene og håndtag pudset",
                            "*Paneler ved stigerne rengjort",
                            "*Batik ryddet op og reoler aftørret",
                        ],
                    ),
                ],
            ),
            (
                "Musiklokalet",
                [
                    (
                        "Musiklokalet",
                        [
                            "Gulvtæpper støvsuget og rettet",
                            "Borde og reoler aftørret",
                            "Vindueskarm aftørret",
                            "*Spindelvæv fjernet",
                        ],
                    ),
                    (
                        "Service og flasker",
                        [
                            "Kollegiets service båret ned, opvask taget",
                            "Flasker og dåser båret ned i ølkælderen eller baggården",
                        ],
                    ),
                ],
            ),
        ],
    ),
    (
        "Kolde trappe",
        "Inkl. kontor · deles af 3 alumner",
        ["Kolde trappe + Kontor"],
        [
            (
                "Trappe",
                [
                    (
                        "Trappe",
                        [
                            "Trappen og fliser i bunden fejet og vasket",
                            "Vindueskarme aftørret",
                            "Gelænderet aftørret",
                            "*Vinduer i gangdørene og håndtag pudset",
                        ],
                    ),
                ],
            ),
            (
                "Kontor",
                [
                    (
                        "Kontor",
                        [
                            "Borde, reoler og låger på personaleskabe aftørret",
                            "Alle håndtag vasket",
                            "Skraldespande tømt",
                            "Vindueskarm aftørret",
                            "Håndvask, toilet, spejl og fliser rengjort",
                            "Gulv i kontor og badeværelser fejet og vasket med grovrens",
                            "*Spindelvæv fjernet",
                            "*Blomster og planter vandet",
                        ],
                    ),
                ],
            ),
        ],
    ),
]
