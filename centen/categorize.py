"""Rule-based categoriser tuned for Belgian bank statements (NL / FR / EN wording).

Every account has a scope: 'personal' (you, your household, joint accounts) or 'business' (your company).

Order of precedence for each transaction:
  1. a category you set by hand            (category_source = 'user', never overwritten)
  2. money to or from an investment, pension or other asset account you added (assets.py): Savings & investments
     when the asset has the same scope as the paying account; a payment from the company into a personal pension
     keeps its category (a cost for the company) and is marked internal = 'cross'. Loans and property only get the
     asset_id, never a new category.
     money between your own accounts or entities:
       same scope  -> Own accounts, or Savings & investments for own-name accounts at brokers
       company <-> private -> Director remuneration, Dividends, Rent, or Company current account
  3. your own rules                        (category_rules table, `centen rules add ...`)
  4. the built-in rules below, top to bottom (some only apply to business or personal accounts)
  5. a fallback based on payment type, direction and scope (credits on a business account = Revenue)
"""
from __future__ import annotations

import re
import sqlite3
from functools import lru_cache

from .money import (find_creditor_id, find_mandate_id, find_structured_ref, mask_iban, normalize_text)

# Bump when rules change: existing data is re-categorised automatically on the next start.
RULES_VERSION = "7"

# (name, kind, group)
CATEGORIES = [
    # income
    ("Salary", "income", "Income"),
    ("Child benefit (Groeipakket)", "income", "Income"),
    ("Healthcare refunds", "income", "Income"),
    ("Tax refund", "income", "Income"),
    ("Benefits & allowances", "income", "Income"),
    ("Interest", "income", "Income"),
    ("Rental income", "income", "Income"),
    ("Refunds", "income", "Income"),
    ("Gifts received", "income", "Income"),
    ("Other income", "income", "Income"),
    # money between your company and you (kind shown for the personal side; see BUSINESS_KIND)
    ("Director remuneration", "income", "Company & private"),
    ("Dividends", "income", "Company & private"),
    ("Company current account", "transfer", "Company & private"),
    # business
    ("Revenue", "income", "Business"),
    ("VAT", "expense", "Business taxes"),
    ("Corporate tax", "expense", "Business taxes"),
    ("Withholding tax", "expense", "Business taxes"),
    ("Payroll & social secretariat", "expense", "Business"),
    ("Accounting & advisory", "expense", "Business"),
    # transfers (excluded from spending and income)
    ("Own accounts", "transfer", "Transfers"),
    ("Household transfers", "transfer", "Transfers"),
    ("Savings & investments", "transfer", "Transfers"),
    ("Credit card bill", "expense", "Transfers"),  # becomes a transfer when a card account is linked
    # expenses
    ("Rent", "expense", "Housing"),
    ("Loans & mortgage", "expense", "Housing"),
    ("Energy", "expense", "Housing"),
    ("Water", "expense", "Housing"),
    ("Telecom & internet", "expense", "Housing"),
    ("Home & DIY", "expense", "Housing"),
    ("Groceries", "expense", "Food"),
    ("Restaurants & takeaway", "expense", "Food"),
    ("Insurance", "expense", "Insurance & health"),
    ("Health insurance (mutualiteit)", "expense", "Insurance & health"),
    ("Healthcare", "expense", "Insurance & health"),
    ("Public transport & taxi", "expense", "Transport"),
    ("Fuel", "expense", "Transport"),
    ("EV charging", "expense", "Transport"),
    ("Parking & tolls", "expense", "Transport"),
    ("Car & maintenance", "expense", "Transport"),
    ("Taxes & social security", "expense", "Taxes"),
    ("Childcare & school", "expense", "Family"),
    ("Subscriptions & streaming", "expense", "Subscriptions"),
    ("News & magazines", "expense", "Subscriptions"),
    ("Shopping", "expense", "Shopping"),
    ("Clothing & shoes", "expense", "Shopping"),
    ("Electronics", "expense", "Shopping"),
    ("Drugstore & personal care", "expense", "Shopping"),
    ("Pets", "expense", "Shopping"),
    ("Leisure & sport", "expense", "Leisure"),
    ("Travel & holidays", "expense", "Leisure"),
    ("Gifts & donations", "expense", "Leisure"),
    ("Education", "expense", "Family"),
    ("Post & shipping", "expense", "Other"),
    ("Bank fees", "expense", "Fees"),
    ("Cash withdrawal", "expense", "Cash"),
    ("Online payments", "expense", "Other"),
    ("Other expenses", "expense", "Other"),
]
KIND = {name: kind for name, kind, _ in CATEGORIES}
GROUP = {name: grp for name, _, grp in CATEGORIES}
# On a business account some categories mean something else: paying your salary is a cost, paying a dividend
# (and the withholding tax on it) is a distribution of profit, not a cost.
BUSINESS_KIND = {"Director remuneration": "expense", "Dividends": "distribution", "Withholding tax": "distribution"}
# Kinds: expense | income | transfer | distribution

# Each rule: (category, direction, [(brand or None, regex on normalised text), ...])
# direction: 'credit' = money in only, 'debit' = money out only, 'any' = both (refunds offset spending).
_MUT = (r"christelijke mutualiteit|helan|solidaris|partenamut|partena ziekenfonds|liberale mutualiteit|"
        r"vlaams (?:&|en) neutraal ziekenfonds|\bvnz\b|onafhankelijk ziekenfonds|mutualite chretienne|"
        r"mutualia|hulpkas|\bhziv\b|\bcaami\b|ziekenfonds|mutualiteit|mutualite|\bmutuelle\b|\bcm (?:ledenbijdrage|bijdrage)")
_FOD = r"fod financien|spf finances|service public federal finances|federale overheidsdienst financien"
_VLABEL = r"vlaamse belastingdienst|\bvlabel\b"

# Each rule: (category, direction, [(brand or None, regex), ...]) or with a 4th element: the scope it applies to.
RULES: list[tuple] = [
    # ---------------- income (credits) ----------------
    ("Dividends", "credit", [(None, r"dividend")], "personal"),
    # Warrant plans: the company buys warrants as remuneration, the plan provider pays the proceeds to the director.
    # (Add your plan provider's name as your own rule, e.g. centen rules add "acme warrants" "Director remuneration".)
    ("Director remuneration", "any", [(None, r"\bwarrants?\b|warrantenplan")]),
    ("Salary", "credit", [
        (None, r"\bloon\b|\bsalaris|\bwedde\b|salaire|remuneration|\bpayroll\b|\bsalary\b|vakantiegeld|"
               r"eindejaarspremie|dertiende maand|13de maand|pecule de vacances|prime de fin d.annee|bezoldiging|"
               r"managementvergoeding|sd ?worx|securex|partena professional|\bgroup s\b|attentia|\beasypay\b|\bsodalis\b"),
    ], "personal"),
    ("Child benefit (Groeipakket)", "credit", [
        ("Groeipakket", r"groeipakket|kinderbijslag|allocations? familiales?|famiwal|\bfons\b|\binfino\b|kidslife|"
                        r"\bparentia\b|myfamily|\bopgroeien\b|\bcamille\b|brussels family|\bfamifed\b"),
    ], "personal"),
    ("Healthcare refunds", "credit", [(None, _MUT + r"|terugbetaling (?:medische|zorg)|remboursement (?:soins|mutuelle)")],
     "personal"),
    ("Tax refund", "credit", [("FOD Financiën", _FOD), ("Vlaamse Belastingdienst", _VLABEL),
                              (None, r"belastingteruggave|teruggave (?:belasting|personenbelasting)|"
                                     r"remboursement (?:d.)?impot|terugbetaling belasting")], "personal"),
    ("Benefits & allowances", "credit", [
        (None, r"\brva\b|\bonem\b|\bhvw\b|\bcapac\b|uitkering|ziektevergoeding|\bindemnite|werkloosheid|chomage|"
               r"federale pensioendienst|\bpensioen\b|\bpension\b|\bacv\b|\babvv\b|\baclvb\b|\bcsc\b|\bfgtb\b|\bcgslb\b|"
               r"ouderschapsverlof|tijdskrediet|loopbaanonderbreking|mijn ?verbouw ?premie|renovatiepremie"),
    ], "personal"),
    ("Interest", "credit", [(None, r"\bintrest|\binterest\b|\brente\b|getrouwheidspremie|basisrente|interets|prime de fidelite")]),
    ("Rental income", "credit", [(None, r"\bhuur\b|huurgeld|\bloyer\b")]),

    # ---------------- transfers ----------------
    ("Savings & investments", "any", [
        ("DEGIRO", r"degiro|flatex"),
        ("LYNX", r"\blynx\b|interactive brokers|\bibkr\b"),
        ("Bitvavo", r"bitvavo"),
        ("Bolero", r"\bbolero\b"),
        ("Keytrade", r"keytrade"),
        ("Saxo", r"\bsaxo\b"),
        ("Trade Republic", r"trade republic"),
        ("MeDirect", r"\bmedirect\b"),
        (None, r"\bcoinbase\b|\bkraken\b|\bbinance\b|\betoro\b|scalable capital|\bbux\b|pensioenspar|langetermijnspar|"
               r"epargne.?pension|spaarrekening|spaarboek|compte d.epargne|\bsavings\b|eigen rekening|naar spaar|van spaar|"
               r"termijnrekening|\bkasbon|spaarplan|beleggingsplan|\bbeleggen\b"),
    ]),
    ("Credit card bill", "debit", [  # only on non-card accounts, see enrich()
        (None, r"(?:afrekening|maandafrekening|uitgaven|settlement|statement|releve|decompte|aflossing|terugbetaling)"
               r".{0,25}(?:kredietkaart|visa|mastercard|creditcard|carte de credit)|"
               r"(?:kredietkaart|visa|mastercard|creditcard|carte de credit).{0,25}"
               r"(?:afrekening|uitgaven|settlement|statement|releve|decompte)|american express|\bamex\b|\bbcc\b"),
    ]),

    # ---------------- business taxes and costs ----------------
    ("VAT", "any", [("BTW", r"\bbtw\b|\btva\b|btw[- ]?ontvangsten|recette (?:de la )?tva")]),
    ("Withholding tax", "debit", [(None, r"roerende voorheffing|precompte mobilier")], "business"),
    ("Corporate tax", "any", [(None, r"vennootschapsbelasting|impot des societes|voorafbetaling|"
                                     r"versements? anticipes?")], "business"),
    ("Payroll & social secretariat", "debit", [(None, r"sd ?worx|securex|partena|acerta|liantis|attentia|\bgroup s\b|"
                                                      r"easypay|sodalis|sociaal secretariaat|secretariat social")], "business"),
    ("Accounting & advisory", "debit", [(None, r"boekhoud|accountan|fiduciaire|fiscalist|comptab|bedrijfsrevisor|"
                                                r"finance (?:&|and|en) tax|\bmoore (?:finance|audit|tax|belgium)")]),

    # ---------------- expenses ----------------
    ("EV charging", "any", [
        (None, r"allego|fastned|ionity|supercharger|tesla charging|shell recharge|blue ?corner|powerdale|\bbonnet\b|"
               r"chargepoint|plugsurfing|newmotion|engie vianeo|totalenergies charging|laadpaal|laadsessie"),
    ]),
    ("Energy", "any", [
        ("Engie", r"\bengie\b|electrabel"),
        ("Luminus", r"luminus"),
        ("TotalEnergies Power & Gas", r"totalenergies (?:power|gas)|total ?energies power|lampiris"),
        ("Eneco", r"\beneco\b"),
        ("Mega", r"\bmega\b(?=.*(?:energie|energy|voorschot|acompte|elektriciteit|electricite|\bgas\b))"),
        ("Octa+", r"octa ?\+|octaplus"),
        ("Bolt Energie", r"bolt energ"),
        ("Ecopower", r"ecopower"),
        ("Fluvius", r"fluvius|eandis|infrax"),
        ("Sibelga", r"sibelga"),
        (None, r"\bores\b|\bresa\b|elegant energie|energie\.be|trevion|frank energie|aspiravi|wase wind|"
               r"vlaams energiebedrijf|energievoorschot|voorschot (?:energie|elektriciteit|gas)|elektriciteit|electricite|"
               r"aardgas|stookolie|mazout|pellets"),
    ]),
    ("Water", "any", [
        ("De Watergroep", r"watergroep"),
        ("Pidpa", r"\bpidpa\b"),
        ("Farys", r"\bfarys\b|\btmvw\b"),
        ("Water-link", r"water-?link"),
        ("Vivaqua", r"vivaqua"),
        (None, r"aquaflanders|\bswde\b|\biwva\b|\binbw\b|\bcile\b|aquafin|waterfactuur|waterrekening|drinkwater|waterverbruik"),
    ]),
    ("Telecom & internet", "any", [
        ("Proximus", r"proximus|scarlet"),
        ("Telenet", r"telenet|\bbase (?:company|mobile)\b|\bbase\.be"),
        ("Orange", r"orange belgium|\borange (?:sa|nv)\b|mobistar"),
        ("Mobile Vikings", r"mobile ?vikings|jim mobile"),
        ("VOO", r"\bvoo\b"),
        ("Digi", r"\bdigi (?:communications|belgium|mobil)"),
        (None, r"hey telecom|youfone|edpnet|lycamobile|unleashed|gsm[- ]?abonnement|internet ?abonnement|mobiel abonnement"),
    ]),
    ("Taxes & social security", "any", [("Xerius", r"\bxerius\b")]),
    ("Health insurance (mutualiteit)", "debit", [(None, _MUT)]),
    ("Insurance", "any", [
        ("AG Insurance", r"\bag insurance\b|\bag employee benefits\b"),
        ("Ethias", r"ethias"),
        ("Baloise", r"baloise|mercator verzekering"),
        ("Belfius Insurance", r"belfius (?:verzekering|insurance|assurance)|\bdvv\b"),
        ("KBC Verzekeringen", r"kbc (?:verzekering|insurance|assurance)"),
        ("AXA", r"\baxa (?:belgium|assurance|verzekering|insurance)"),
        ("P&V", r"\bp ?& ?v\b|\bvivium\b"),
        ("Allianz", r"allianz"),
        ("Federale Verzekering", r"federale (?:verzekering|assurance)"),
        ("Corona Direct", r"corona direct"),
        ("Fidea", r"\bfidea\b"),
        ("VAB", r"\bvab\b"),
        ("Touring", r"\btouring\b"),
        (None, r"argenta (?:assuranties|verzekering)|yuzzu|\bnn (?:insurance|life|verzekering)|\baedes\b|"
               r"\bdkv\b|europ assistance|verzekering(?!sfonds)|"
               r"assurance|insurance|\bpolis|familiale|brandverzekering|autoverzekering|hospitalisatie"),
    ]),
    ("Groceries", "any", [
        ("Colruyt", r"colruyt"),
        (None, r"\bvoeding\b"),
        ("Okay", r"\bokay (?:compact|supermarkt)\b|^okay\b"),
        ("Bio-Planet", r"bio[- ]?planet"),
        ("Delhaize", r"delhaize|shop.?n.?go"),
        ("Carrefour", r"carrefour"),
        ("Aldi", r"\baldi\b"),
        ("Lidl", r"\blidl\b"),
        ("Albert Heijn", r"albert heijn|\bah to go\b"),
        ("Intermarché", r"intermarche"),
        ("Spar", r"\bspar\b"),
        ("Jumbo", r"\bjumbo\b"),
        ("Makro", r"\bmakro\b"),
        ("Picnic", r"\bpicnic\b"),
        ("Collect&Go", r"collect ?(?:&|and|en) ?go"),
        (None, r"\bsmatch\b|renmans|\balvo\b|\bprofi\b|\bhanos\b|\bcrf (?:market|express)|supermarkt|supermarche|"
               r"slagerij|boucherie|beenhouwerij|bakkerij|boulangerie|\bbakker\b|kaasmakerij|groentewinkel|primeur"),
    ]),
    ("Restaurants & takeaway", "any", [
        ("Deliveroo", r"deliveroo"),
        ("Uber Eats", r"uber ?eats"),
        ("Takeaway.com", r"takeaway\.com|thuisbezorgd|just ?eat"),
        ("McDonald's", r"mc ?donald"),
        ("Quick", r"\bquick restaurants?\b|^quick\b"),
        ("Burger King", r"burger ?king"),
        ("Starbucks", r"starbucks"),
        (None, r"\bkfc\b|\bexki\b|\bpanos\b|pain quotidien|pizza ?hut|domino.?s|lunch ?garden|ellis gourmet|"
               r"balls ?(?:&|and) ?glory|sushi ?shop|restaurant|\bresto\b|brasserie|bistro|taverne|frituur|friterie|"
               r"frietkot|snackbar|pizzeria|sushi|kebab|eetcafe|trattoria|cafetaria|koffiebar|coffee|\bcafe\b"),
    ]),
    ("Rent", "debit", [(None, r"\bhuur\b|huurgeld|\bloyer\b|huishuur|maandhuur")]),
    ("Loans & mortgage", "debit", [
        (None, r"woonkrediet|hypothe(?:ek|caire)|pret hypothecaire|credit hypothecaire|mortgage|aflossing|"
               r"terugbetaling (?:lening|krediet)|\blening\b|autolening|persoonlijke lening|\bkrediet\b|\bleasing\b"),
    ]),
    ("Public transport & taxi", "any", [
        ("NMBS", r"\bnmbs\b|\bsncb\b|belgian rail"),
        ("De Lijn", r"de lijn"),
        ("MIVB/STIB", r"\bstib\b|\bmivb\b"),
        ("TEC", r"\btec\b"),
        ("Eurostar", r"eurostar|thalys"),
        ("Uber", r"\buber\b"),
        ("Bolt", r"\bbolt\b"),
        (None, r"blue-?bike|\bns (?:groep|reizigers|international)\b|\bsncf\b|deutsche bahn|\bpoppy\b|cambio|\btaxi\b"),
    ]),
    ("Parking & tolls", "any", [
        ("4411", r"\b4411\b"),
        ("Parkmobile", r"parkmobile"),
        ("Interparking", r"interparking"),
        (None, r"q-?park|\bindigo\b|yellowbrick|\bparko\b|be-?mobile|easypark|\bparking\b|parkeer|parkeren|viapass|"
               r"liefkenshoek|\btunnel\b|\bpeage\b|\btoll\b|vignet|telepass|lage.?emissiezone"),
    ]),
    ("Fuel", "any", [
        ("Q8", r"\bq8\b"),
        ("Shell", r"\bshell\b"),
        ("Esso", r"\besso\b"),
        ("TotalEnergies", r"totalenergies|\btotal (?:station|belgium)"),
        ("DATS 24", r"\bdats ?24\b"),
        (None, r"lukoil|texaco|gabriels|\bavia\b|\bargos\b|tankstation|station.?service|carburant|brandstof|diesel|benzine"),
    ]),
    ("Car & maintenance", "any", [
        (None, r"kwik-?fit|carglass|autoglass|euromaster|vulco|norauto|car ?wash|carwash|autowas|autokeuring|\bkeuring\b|"
               r"\bgoca\b|autosecurite|\bgarage\b|carrosserie|bandencentrale|porsche|d.ieteren|volkswagen|\baudi\b|\bbmw\b|"
               r"car finance|leaseplan|ayvens|\barval\b|athlon|"
               r"mercedes|toyota|\btesla\b|\bskoda\b|renault|peugeot|citroen|\bkia\b|hyundai|\bvolvo\b"),
    ]),
    ("Taxes & social security", "any", [
        ("FOD Financiën", _FOD),
        ("Vlaamse Belastingdienst", _VLABEL),
        (None, r"sociaal verzekeringsfonds|caisse d.assurances sociales|\bprovincie\b|\bbelasting|\bimpot|precompte|voorheffing|voorafbetaling|verkeersbelasting|taxe de circulation|"
               r"inschrijvingstaks|gemeentebelasting|gemeentetaks|provinciebelasting|afvalbelasting|milieubelasting|"
               r"sociale bijdrage|cotisations? sociales?|\bxerius\b|\bacerta\b|\bliantis\b|\bzenito\b|\bucm\b|\bentrepia\b|"
               r"\brsvz\b|\binasti\b|\bdivio\b|\bboete\b|\bamende\b|gas-?boete"),
    ]),
    ("Childcare & school", "any", [
        (None, r"kinderopvang|kinderdagverblijf|\bkdv\b|creche|onthaalouder|gastouder|\bibo\b|buitenschoolse|naschoolse|"
               r"speelplein|kleuterschool|basisschool|lagere school|\bschool\b|scholengroep|schoolrekening|schoolfactuur|"
               r"\becole\b|middagmaal|refter|jeugdbeweging|\bchiro\b|\bscouts\b|\bksa\b|grabbelpas|sportkamp"),
    ]),
    ("Subscriptions & streaming", "any", [
        ("Netflix", r"netflix"),
        ("Spotify", r"spotify"),
        ("Disney+", r"disney ?\+|disneyplus|disney plus"),
        ("Streamz", r"streamz"),
        ("VTM GO", r"vtm ?go"),
        ("Amazon Prime", r"prime ?video|amazon prime|amzn prime|primevideo"),
        ("Apple", r"apple\.com/bill|apple services|itunes|apple one|icloud"),
        ("YouTube Premium", r"youtube ?(?:premium|music)"),
        ("Google", r"google (?:one|play|storage|workspace|\*)"),
        ("HBO Max", r"hbo ?max|\bmax\.com"),
        ("Deezer", r"deezer"),
        ("Audible", r"audible"),
        ("Storytel", r"storytel"),
        ("OpenAI", r"openai|chatgpt"),
        ("Anthropic", r"anthropic|claude\.ai"),
        ("Microsoft", r"microsoft ?(?:365|\*|store|office)|xbox|game ?pass"),
        ("Adobe", r"\badobe\b"),
        ("Dropbox", r"dropbox"),
        ("PlayStation", r"playstation|sony interactive"),
        ("Nintendo", r"nintendo"),
        ("Pickx", r"pickx"),
        ("DAZN", r"\bdazn\b"),
        (None, r"\bcanva\b|patreon|twitch|play sports|eleven (?:sports|pro league)|nordvpn|expressvpn|surfshark|"
               r"1password|lastpass|bitwarden|duolingo|strava|\bkobo\b"),
    ]),
    ("News & magazines", "any", [
        (None, r"mediahuis|dpg media|de standaard|nieuwsblad|gazet van antwerpen|belang van limburg|\bhbvl\b|"
               r"het laatste nieuws|\bhln\b|de morgen|\bde tijd\b|l.echo|le soir|la libre|rossel|roularta|\bknack\b|"
               r"\bhumo\b|dag allemaal|pressreader|blendle"),
    ]),
    ("Electronics", "any", [
        ("Coolblue", r"coolblue"),
        ("MediaMarkt", r"media ?markt"),
        ("Vanden Borre", r"vanden ?borre"),
        ("Krëfel", r"krefel"),
        ("Fnac", r"\bfnac\b"),
        (None, r"apple store|\bsamsung\b|\bdyson\b|alternate\.be|megekko|belsimpel|gsm ?shop|refurbed"),
    ]),
    ("Clothing & shoes", "any", [
        ("Zalando", r"zalando"),
        ("Vinted", r"vinted"),
        (None, r"\bh ?& ?m\b|\bhm\.com|primark|\bc ?& ?a\b|\bjbc\b|zeeman|\bzara\b|bershka|pull ?& ?bear|massimo dutti|"
               r"\bmango\b|\bwe fashion|scapino|torfs|\bbristol\b|brantano|shoe discount|\bsacha\b|\bnike\b|adidas|"
               r"uniqlo|esprit|\bshein\b|superdry|jack ?& ?jones|vero moda|terre bleue|\bcks\b|\bzeb\b|kiabi|noppies|dreambaby"),
    ]),
    ("Home & DIY", "any", [
        ("IKEA", r"\bikea\b"),
        ("Brico", r"\bbrico\b|brico ?plan-?it"),
        ("Gamma", r"\bgamma\b"),
        ("Hubo", r"\bhubo\b"),
        ("Hornbach", r"hornbach"),
        ("HEMA", r"\bhema\b"),
        ("Action", r"^action\b|\baction (?:nv|belgium|store)\b"),
        (None, r"leroy merlin|mr\.? ?bricolage|\bpraxis\b|kwantum|\bjysk\b|\bxenos\b|blokker|dille ?& ?kamille|"
               r"maisons du monde|loods ?5|\baveve\b|intratuin|tuincentrum|\bx2o\b|\bfacq\b|sanitair|keukenbouw|meubel"),
    ]),
    ("Drugstore & personal care", "any", [
        ("Kruidvat", r"kruidvat"),
        (None, r"ici paris|planet parfum|\bdouglas\b|yves rocher|\blush\b|the body shop|medi-?market|\betos\b|drogist|"
               r"\bkapper|kapsalon|coiffure|coiffeur|hairdresser|barber|nagelstudio|schoonheidssalon|wellness|massage"),
    ]),
    ("Pets", "any", [
        (None, r"tom ?& ?co|maxi ?zoo|zooplus|dierenarts|veterinai|dierenspeciaalzaak|hondenschool|dierenvoeding"),
    ]),
    ("Healthcare", "any", [
        (None, r"apotheek|pharmacie|pharmacy|multipharma|\bdokter\b|huisarts|tandarts|dentist|ziekenhuis|hopital|hospital|"
               r"\buz (?:leuven|gent|antwerpen|brussel)\b|\baz [a-z]+|\bchu\b|kinesi|\bkine\b|osteopa|psycholo|opticien|"
               r"optiek|pearle|hans anders|specsavers|lensonline|logopedi|\blabo\b|laboratorium|consultatie|vroedvrouw"),
    ]),
    ("Leisure & sport", "any", [
        ("Kinepolis", r"kinepolis"),
        ("Basic-Fit", r"basic-?fit"),
        (None, r"\bugc\b|\bpathe\b|cinema|bioscoop|sportoase|\bjims\b|sportcity|fitness|\bgym\b|ticketmaster|sportpaleis|"
               r"ticketswap|see tickets|eventbrite|plopsa|walibi|bobbejaanland|pairi daiza|planckendael|zoo antwerpen|"
               r"efteling|museum|musee|\bpadel|\bgolf\b|tennis|zwembad|piscine|sportclub|voetbalclub|lidgeld|concert|"
               r"theater|theatre|steampowered|steam games|epic games|bowling|lasergame|escape ?room|klimzaal|boulder"),
    ]),
    ("Travel & holidays", "any", [
        ("Booking.com", r"booking\.com"),
        ("Airbnb", r"airbnb"),
        ("Ryanair", r"ryanair"),
        ("Brussels Airlines", r"brussels airlines"),
        ("TUI", r"\btui\b"),
        (None, r"transavia|\bklm\b|lufthansa|easyjet|wizz ?air|vueling|iberia|air france|expedia|hotels\.com|\bhotel\b|"
               r"sunweb|center ?parcs|\blandal\b|vayamundo|jetair|neckermann|corendon|skyscanner|trivago|agoda|vakantie|"
               r"camping|hostel|bed ?& ?breakfast|\bairport\b|luchthaven|\bsixt\b|\bhertz\b|europcar|\bavis\b"),
    ]),
    ("Gifts & donations", "any", [
        (None, r"rode kruis|croix-rouge|11\.11\.11|artsen zonder grenzen|medecins sans frontieres|kom op tegen kanker|"
               r"stichting tegen kanker|\bunicef\b|natuurpunt|\bwwf\b|\boxfam\b|greenpeace|amnesty|plan international|"
               r"\bdonatie|schenking|welzijnsschakels|music for life|warmste week|kinderkankerfonds|\bcadeau"),
    ]),
    ("Education", "any", [
        (None, r"\bsyntra\b|\bcvo\b|opleiding|\bcursus|\bformation\b|academie|hogeschool|universiteit|ku ?leuven|\bugent\b|"
               r"uantwerpen|\bvub\b|uclouvain|\bulb\b|inschrijvingsgeld|minerval|udemy|coursera|linkedin learning"),
    ]),
    ("Post & shipping", "any", [("bpost", r"\bbpost\b"), (None, r"postnl|\bdhl\b|\bups\b|\bgls\b|\bdpd\b|postzegel|sendcloud")]),
    ("Shopping", "any", [
        ("bol.com", r"\bbol\.com\b|\bbol com\b"),
        ("Amazon", r"\bamazon\b|\bamzn\b"),
        ("AliExpress", r"aliexpress"),
        ("Temu", r"\btemu\b"),
        (None, r"standaard boekhandel|\bclub boekhandel|dreamland|bart smit|intertoys|\bebay\b|\betsy\b|webshop|"
               r"corporate benefi"),
    ]),
    ("Bank fees", "debit", [
        (None, r"beheerskosten|kosten (?:rekening|pakket|kaart|beheer|verrichting)|maandelijkse bijdrage|jaarlijkse bijdrage|"
               r"bijdrage (?:pakket|rekening|kaart)|frais de (?:gestion|tenue)|cotisation (?:mensuelle|annuelle)|debetrente|"
               r"debetintrest|intrest(?:en)? debet|aanrekening kosten|transactiekosten|wisselkosten|koersverschil|"
               r"\bcommissie\b|account fee|card fee|\bforfait (?:mensuel|bancaire)"),
    ]),
    ("Cash withdrawal", "debit", [
        (None, r"geldopneming|geldopname|geldafhaling|retrait|cash withdrawal|geldautomaat|\batm\b|batopin"),
    ]),
    ("Refunds", "credit", [(None, r"terugbetaling|remboursement|\brefund|\bretour|creditnota|credit ?note|terugstorting|cashback")]),
    ("Online payments", "any", [
        ("PayPal", r"paypal"),
        ("Klarna", r"klarna"),
        (None, r"payconiq|\bmollie\b|\bstripe\b|\bsumup\b|\badyen\b|\bpay\.nl\b|buckaroo|multisafepay"),
    ]),
]

# Card processors that sometimes appear as the counterparty instead of the merchant.
_PROCESSORS = re.compile(r"worldline|bancontact|mastercard|\bvisa\b|\bccv\b|adyen|stripe|sumup|mollie|payconiq|ingenico|"
                         r"six payment|equens|\batos\b|\bpaypal\b")


@lru_cache(maxsize=None)
def _compiled():
    out = []
    for rule in RULES:
        category, direction, pats = rule[:3]
        scope = rule[3] if len(rule) > 3 else None
        out.append((category, direction, scope, [(brand, re.compile(rx)) for brand, rx in pats]))
    return out


# --- payment type -------------------------------------------------------------

def detect_payment_type(text_norm: str, bank_tx_code: str | None = None) -> str:
    code = (bank_tx_code or "").upper()
    if any(k in code for k in ("ESDD", "BBDD", "RDDT", "IDDT", "DDBT")):
        return "direct_debit"
    if "CWDL" in code:
        return "cash"
    if any(k in code for k in ("CCRD", "POSD", "POSC", "CDPT")):
        return "card"
    if "STDO" in code:
        return "standing_order"
    if "CHRG" in code or "FEES" in code:
        return "fee"
    t = text_norm
    if re.search(r"domicili|sepa direct debit|prelevement|\bsdd\b|mandaat|mandat\b", t):
        return "direct_debit"
    if re.search(r"geldopneming|geldopname|geldafhaling|retrait|cash withdrawal|geldautomaat|\batm\b|batopin", t):
        return "cash"
    if re.search(r"betaling (?:met|via)|aankoop (?:met|via)|aankoop kredietkaart|kredietkaart aankoop|bancontact|maestro|debetkaart|debit ?card|kaartbetaling|"
                 r"paiement (?:par|avec|via) carte|achat (?:par|avec) carte|apple pay|google pay|contactloos|sans contact|"
                 r"\bv ?pay\b|\bpos\b|visa debit", t):
        return "card"
    if re.search(r"doorlopende opdracht|ordre permanent|standing order|permanente opdracht", t):
        return "standing_order"
    if re.search(r"instant", t):
        return "instant"
    if re.search(r"overschrijving|virement|\btransfer\b|credit transfer", t):
        return "transfer"
    return "other"


# --- merchant -------------------------------------------------------------------

_BOILER = [re.compile(p) for p in (
    r"betaling (?:met|via) (?:debetkaart|bancontact|maestro|kaart|apple pay|google pay|visa debit|uw kaart)",
    r"aankoop (?:met|via) (?:bancontact|debetkaart|kaart)", r"aankoop kredietkaart", r"achat carte de credit",
    r"paiement (?:par|avec|via) (?:carte|bancontact|maestro)(?: bancaire)?",
    r"achat (?:par|avec) carte(?: bancaire)?",
    r"europese domiciliering", r"domiciliering", r"domiciliation(?: europeenne)?", r"sepa direct debit",
    r"europese overschrijving(?: (?:van|naar))?", r"overschrijving (?:van|naar|ten gunste van)", r"instantoverschrijving",
    r"virement (?:europeen|instantane)?(?: (?:de|vers|en faveur de))?", r"doorlopende opdracht", r"ordre permanent",
    r"apple pay", r"google pay", r"contactloos", r"sans contact", r"bancontact", r"maestro", r"visa debit", r"debetkaart",
    r"kaart(?:nummer|nr)?\s*[\dx* ]{4,}", r"\b\d{4}\s?[x*]{2,}[\sx*\d]*", r"\b[x*]{4,}\d*",
    r"\b\d{2}[/.-]\d{2}(?:[/.-]\d{2,4})?\b", r"\b\d{1,2}[:h.]\d{2}(?:[:.]\d{2})?\b",
    r"\b(?:ref|referentie|reference|nr)\b\s*[:.]?\s*\S+", r"\bvaluta(?:datum)?\b.*", r"\buitgevoerd op\b.*",
    r"\bmededeling\s*:?", r"\bbic\s*:?\s*\S+", r"\b[a-z]{2}\d{2}(?:\s?[a-z0-9]{4}){3,7}\b",
    r"[+*]{3}[\d/ ]+[+*]{3}", r"\b\d+[.,]\d{2}\s*(?:eur)?\b", r"\beur\b", r"\b\d{4,}\b",
    r"\b(?:bel|be|nld|nl|fra|fr|deu|lux|lu)\s*$",
)]
_STOP = {"bv", "bvba", "nv", "sa", "srl", "sprl", "vzw", "asbl", "cv", "cvba", "comm", "gcv", "ltd", "inc", "gmbh",
         "bel", "be", "belgium", "belgie", "belgique", "de", "het", "la", "le", "les", "van", "der", "den", "op", "en",
         "et", "the", "di", "du", "des", "a", "b"}


_GENERIC = {"pizzeria", "frituur", "friterie", "restaurant", "resto", "brasserie", "bistro", "cafe", "taverne", "bakkerij",
            "bakker", "boulangerie", "slagerij", "beenhouwerij", "boucherie", "apotheek", "pharmacie", "garage", "kapper",
            "kapsalon", "coiffure", "hotel", "parking", "tankstation", "snack", "snackbar", "sushi", "kebab", "eethuis",
            "kinderdagverblijf", "dokter", "tandarts", "praktijk", "school", "basisschool", "vzw", "fietsen", "bloemen",
            "broodjes", "traiteur", "wijnhandel", "drukkerij", "optiek", "opticien", "dierenarts"}
_LEGAL = re.compile(r"(?:\s+(?:nv|bv|sa|srl|sprl|bvba|vzw|asbl|cv|cvba|ag|gmbh|ltd|inc|bank nv))+$")


def _clean_name(s: str) -> str:
    t = normalize_text(s)
    for rx in _BOILER:
        t = rx.sub(" ", t)
    t = re.sub(r"[^\w&'.+\- ]", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" .-'")
    t = _LEGAL.sub("", t) or t
    return t


_LOWER_WORDS = {"de", "het", "la", "le", "les", "van", "den", "der", "op", "en", "et", "du", "des", "bij", "aan", "the",
                "of", "and", "da", "di", "del", "dei", "t", "s"}


def _title(s: str) -> str:
    words = []
    for w in s.split():
        if len(w) <= 3 and w.isalpha() and w not in _LOWER_WORDS and not re.search(r"[aeiouy]", w[1:]):
            words.append(w.upper())  # KBC, AG, BNP, NMBS-like acronyms
        else:
            words.append(w.capitalize())
    return " ".join(words)[:48]


def extract_merchant(counterparty_name: str | None, description: str | None, remittance: str | None) -> str | None:
    cp = (counterparty_name or "").strip()
    if cp and not _PROCESSORS.search(normalize_text(cp)):
        cleaned = _clean_name(cp)
        if cleaned:
            return _title(cleaned)
    for src in (description, remittance):
        cleaned = _clean_name(src or "")
        if cleaned:
            return _title(" ".join(cleaned.split()[:5]))
    return _title(_clean_name(cp)) if cp else None


def merchant_key_for(merchant: str | None, brand: str | None = None, creditor_id: str | None = None,
                     counterparty_iban: str | None = None) -> str:
    if brand:
        return normalize_text(brand)
    if creditor_id:
        return "cid:" + creditor_id
    toks = [w for w in re.split(r"[^a-z0-9]+", normalize_text(merchant or "")) if w and w not in _STOP and not w.isdigit()]
    if toks:
        if toks[0] in _GENERIC:  # "pizzeria da mario" vs "pizzeria roma"
            return " ".join(toks[:3])
        return toks[0] if len(toks[0]) >= 4 or len(toks) == 1 else " ".join(toks[:2])
    if counterparty_iban:
        return "iban:" + counterparty_iban
    return "unknown"


# --- categorise -------------------------------------------------------------------

_LEGAL_FORMS = {"bv", "nv", "bvba", "srl", "sa", "sprl", "vof", "commv", "cv", "cvba", "scrl", "scs", "snc", "ltd",
                "gmbh", "bvba", "vzw", "asbl", "sc"}
# Own-name accounts at these banks are broker cash accounts: (country, bank code) -> name.
_BROKER_BANK_CODES = {("DE", "10130800"): "DEGIRO", ("NL", "FLTX"): "DEGIRO"}


def broker_from_iban(iban: str | None) -> str | None:
    if not iban:
        return None
    return _BROKER_BANK_CODES.get((iban[:2], iban[4:12])) or _BROKER_BANK_CODES.get((iban[:2], iban[4:8]))


_BROKERS = re.compile(r"degiro|flatex|\blynx\b|interactive brokers|bitvavo|bolero|keytrade|\bsaxo\b|trade republic|"
                      r"medirect|scalable|\betoro\b|coinbase|kraken|binance|revolut securities")


def _tokens(name: str | None) -> frozenset:
    return frozenset(w for w in re.split(r"[^a-z0-9]+", normalize_text(name or "")) if w and w not in _LEGAL_FORMS)


def entity_name(name: str | None) -> str:
    """'Acme Consulting BV' -> 'acme consulting'."""
    return " ".join(w for w in re.split(r"[^a-z0-9]+", normalize_text(name or "")) if w and w not in _LEGAL_FORMS)


def detect_scope(name: str | None) -> str:
    """Accounts held by a company (name contains BV, NV, BVBA, SRL, ...) are business; everything else personal."""
    words = set(re.split(r"[^a-z0-9]+", normalize_text(name or "")))
    return "business" if words & (_LEGAL_FORMS - {"sc", "cv"}) else "personal"


def _compile_user_pattern(pattern: str) -> re.Pattern:
    pat = normalize_text(pattern)
    try:
        return re.compile(pat)
    except re.error:
        return re.compile(re.escape(pat))


# Asset types whose payments are savings, not spending. Loans and property keep their category.
INVESTING_ASSET_TYPES = {"investment", "crypto", "pension", "insurance", "asset"}


class Context:
    def __init__(self, own_ibans=(), user_rules=(), has_card_account=False, card_accounts=(), accounts=(),
                 members=None, assets=()):
        """accounts: iterable of dicts with id, iban, name, scope. members: {name: [full names as banks write them]}.
        assets: iterable of dicts with id, name, scope, type, ibans, patterns (investment / pension / loan accounts)."""
        self.asset_ibans: dict[str, dict] = {}
        self.asset_patterns: list[tuple[re.Pattern, dict]] = []
        for a in assets:
            for i in a.get("ibans") or ():
                self.asset_ibans[i] = a
            for pat in a.get("patterns") or ():
                self.asset_patterns.append((_compile_user_pattern(pat), a))
        self.members: list[tuple[str, set[frozenset]]] = []
        for mname, aliases in (members or {}).items():
            sets = {_tokens(a) for a in (aliases or []) if len(_tokens(a)) >= 2}
            if len(_tokens(mname)) >= 2:
                sets.add(_tokens(mname))
            if sets:
                self.members.append((mname, sets))
        self.card_accounts = set(card_accounts)
        self.has_card_account = has_card_account
        self.account_scope: dict[str, str] = {}
        self.iban_scope: dict[str, str] = {i: "personal" for i in own_ibans}
        self.business_names: set[str] = set()
        self.personal_names: set[frozenset] = set()
        for a in accounts:
            scope = a.get("scope") or detect_scope(a.get("name"))
            self.account_scope[a["id"]] = scope
            if a.get("iban"):
                self.iban_scope[a["iban"]] = scope
            if scope == "business":
                if entity_name(a.get("name")):
                    self.business_names.add(entity_name(a.get("name")))
            else:
                toks = _tokens(a.get("name"))
                if len(toks) >= 2:
                    self.personal_names.add(toks)
        self.own_ibans = set(self.iban_scope)
        self.user_rules = []
        for r in user_rules:
            self.user_rules.append((_compile_user_pattern(r["pattern"]), r["direction"] or "any", r["category"],
                                    r["merchant"]))

    @classmethod
    def from_db(cls, conn: sqlite3.Connection) -> "Context":
        accounts = [dict(r) for r in conn.execute("SELECT id, iban, name, scope, account_type FROM accounts "
                                                  "WHERE source != 'asset'")]
        assets = {r["id"]: {"id": r["id"], "name": r["name"], "scope": r["scope"] or "personal",
                            "type": r["account_type"], "ibans": [], "patterns": []}
                  for r in conn.execute("SELECT id, name, scope, account_type FROM accounts WHERE source='asset'")}
        for r in conn.execute("SELECT account_id, kind, iban, pattern FROM asset_links"):
            a = assets.get(r["account_id"])
            if a is not None:
                (a["ibans"].append(r["iban"]) if r["kind"] == "iban" else a["patterns"].append(r["pattern"]))
        rules = conn.execute("SELECT pattern, direction, category, merchant FROM category_rules ORDER BY id DESC").fetchall()
        cards = {a["id"] for a in accounts if a["account_type"] == "card"}
        from . import config  # local import keeps categorize free of side effects at import time
        try:
            members = config.load().get("household_members") or {}
        except Exception:  # noqa: BLE001 - a broken config must not stop categorisation
            members = {}
        return cls((), [dict(r) for r in rules], bool(cards), cards, accounts, members, assets.values())

    def asset_for(self, tx: dict, text: str) -> dict | None:
        """The asset account a payment goes to or comes from: by counterparty account, else by text pattern
        (also matched against the broker name derived from the IBAN, e.g. DEGIRO's flatex accounts)."""
        iban = tx.get("counterparty_iban")
        if iban and iban in self.asset_ibans:
            return self.asset_ibans[iban]
        if self.asset_patterns:
            hay = f"{text} {normalize_text(broker_from_iban(iban) or '')}"
            for rx, a in self.asset_patterns:
                if rx.search(hay):
                    return a
        return None

    def member_for(self, name: str | None) -> str | None:
        toks = _tokens(name)
        for mname, sets in self.members:
            if toks in sets:
                return mname
        return None

    def scope_of(self, account_id: str | None) -> str:
        return self.account_scope.get(account_id or "", "personal")


def _dir_ok(direction: str, amount: float) -> bool:
    return direction == "any" or (direction == "credit" and amount > 0) or (direction == "debit" and amount < 0)


def kind_for(category: str, ctx: Context | None = None, scope: str = "personal") -> str:
    if category == "Credit card bill" and ctx is not None and ctx.has_card_account:
        return "transfer"
    if scope == "business" and category in BUSINESS_KIND:
        return BUSINESS_KIND[category]
    return KIND.get(category, "expense")


def _counterparty_entity(tx: dict, ctx: Context) -> tuple[str | None, bool]:
    """Is the counterparty one of your own accounts or entities? Returns (scope, linked)."""
    iban = tx.get("counterparty_iban")
    if iban and iban in ctx.iban_scope:
        return ctx.iban_scope[iban], True
    name = tx.get("counterparty_name")
    if name:
        ent = entity_name(name)
        if ent and any(ent == b or ent.startswith(b + " ") for b in ctx.business_names):
            return "business", False
        toks = _tokens(name)
        if len(toks) >= 2 and toks in ctx.personal_names:
            return "personal", False
    return None, False


_REMUNERATION = re.compile(r"verloning|\bloon\b|bezoldiging|remunerat|salaris|\bwedde\b|management ?(?:fee|vergoeding)|"
                           r"salaire|bedrijfsleidersvergoeding")


def _internal_category(tx: dict, ctx: Context, scope: str, text: str) -> tuple[str | None, str | None, str | None]:
    """(category, internal, merchant) for money between your own accounts / between your company and you."""
    target, linked = _counterparty_entity(tx, ctx)
    if not linked and scope == "personal":
        member = ctx.member_for(tx.get("counterparty_name"))
        if member:
            return "Household transfers", "own", member
    if not target:
        return None, None, None
    amount = float(tx["amount"])
    if target == scope:
        iban = tx.get("counterparty_iban")
        if not linked and scope == "personal" and ((iban and not iban.startswith("BE")) or _BROKERS.search(text)):
            name = broker_from_iban(iban) or (f"Own account {mask_iban(iban)}" if iban else None)
            return "Savings & investments", "own", name
        return "Own accounts", "own", None
    # company <-> private
    if _REMUNERATION.search(text):
        return "Director remuneration", "cross", None
    if "dividend" in text:
        return "Dividends", "cross", None
    if re.search(r"\bhuur\b|\bloyer\b", text):
        return ("Rental income" if amount > 0 else "Rent"), "cross", None
    return "Company current account", "cross", None


def enrich(tx: dict, ctx: Context) -> dict:
    """Fill payment_type, identifiers, merchant, merchant_key, category, kind, internal (in place) and return tx."""
    amount = float(tx["amount"])
    scope = ctx.scope_of(tx.get("account_id"))
    raw_text = " | ".join(x for x in (tx.get("counterparty_name"), tx.get("description"), tx.get("remittance")) if x)
    text = normalize_text(raw_text)

    tx["structured_ref"] = tx.get("structured_ref") or find_structured_ref(raw_text)
    tx["mandate_id"] = tx.get("mandate_id") or find_mandate_id(raw_text)
    tx["creditor_id"] = tx.get("creditor_id") or find_creditor_id(raw_text)
    tx["payment_type"] = tx.get("payment_type") or detect_payment_type(text, tx.get("bank_tx_code"))
    if tx["payment_type"] in ("other", "transfer") and tx.get("mandate_id"):
        tx["payment_type"] = "direct_debit"

    asset = ctx.asset_for(tx, text)
    tx["asset_id"] = asset["id"] if asset else None
    if asset and asset["type"] in INVESTING_ASSET_TYPES and asset["scope"] == scope:
        category, internal, brand = "Savings & investments", "own", asset["name"]
    else:
        category, internal, brand = _internal_category(tx, ctx, scope, text)
        if asset and asset["scope"] != scope and asset["type"] in INVESTING_ASSET_TYPES and not internal:
            internal = "cross"  # e.g. the company paying into your personal pension: a cost for it, savings for you
    source = "rule"
    if not category:
        for rx, direction, cat, merch in ctx.user_rules:
            if _dir_ok(direction, amount) and rx.search(text):
                category, brand = cat, merch
                break
    if not category:
        for cat, direction, rule_scope, pats in _compiled():
            if not _dir_ok(direction, amount) or (rule_scope and rule_scope != scope):
                continue
            if cat == "Credit card bill" and tx.get("account_id") in ctx.card_accounts:
                continue
            for b, rx in pats:
                if rx.search(text):
                    category, brand = cat, b
                    break
            if category:
                break
    if tx.get("category_source") == "user" and tx.get("category"):
        pass  # keep the hand-picked category; the brand match above is only used for the merchant name
    else:
        if not category:
            source = "default"
            if tx["payment_type"] == "cash" and amount < 0:
                category = "Cash withdrawal"
            elif tx["payment_type"] == "fee" and amount < 0:
                category = "Bank fees"
            elif amount > 0:
                category = "Revenue" if scope == "business" else "Other income"
            else:
                category = "Other expenses"
        tx["category"], tx["category_source"] = category, source

    tx["internal"] = internal
    tx["kind"] = kind_for(tx["category"], ctx, scope)
    tx["merchant"] = brand or extract_merchant(tx.get("counterparty_name"), tx.get("description"), tx.get("remittance"))
    tx["merchant_key"] = merchant_key_for(tx["merchant"], brand, tx.get("creditor_id"), tx.get("counterparty_iban"))
    return tx


def recategorize(conn: sqlite3.Connection) -> int:
    """Re-run the rules over every transaction (hand-picked categories are kept)."""
    ctx = Context.from_db(conn)
    rows = conn.execute("SELECT * FROM transactions").fetchall()
    updates = []
    for r in rows:
        tx = dict(r)
        if tx.get("category_source") != "user":
            tx["category"] = None
            tx["category_source"] = None
        tx["payment_type"] = None
        enrich(tx, ctx)
        updates.append((tx["payment_type"], tx["structured_ref"], tx["mandate_id"], tx["creditor_id"], tx["merchant"],
                        tx["merchant_key"], tx["category"], tx["category_source"], tx["kind"], tx["internal"],
                        tx.get("asset_id"), tx["id"]))
    conn.executemany(
        """UPDATE transactions SET payment_type=?, structured_ref=?, mandate_id=?, creditor_id=?, merchant=?,
           merchant_key=?, category=?, category_source=?, kind=?, internal=?, asset_id=? WHERE id=?""", updates)
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('rules_version', ?)", (RULES_VERSION,))
    conn.commit()
    return len(updates)


def set_category(conn: sqlite3.Connection, tx_id: str, category: str) -> dict:
    if category not in KIND:
        raise ValueError(f"Unknown category '{category}'. Known: {', '.join(KIND)}")
    ctx = Context.from_db(conn)
    row = conn.execute("SELECT account_id FROM transactions WHERE id=?", (tx_id,)).fetchone()
    if not row:
        raise ValueError(f"No transaction with id {tx_id}")
    conn.execute("UPDATE transactions SET category=?, category_source='user', kind=? WHERE id=?",
                 (category, kind_for(category, ctx, ctx.scope_of(row[0])), tx_id))
    conn.commit()
    return {"id": tx_id, "category": category}


def add_rule(conn: sqlite3.Connection, pattern: str, category: str, direction: str = "any",
             merchant: str | None = None) -> dict:
    if category not in KIND:
        raise ValueError(f"Unknown category '{category}'. Known: {', '.join(KIND)}")
    if direction not in ("any", "debit", "credit"):
        raise ValueError("direction must be any, debit or credit")
    conn.execute("INSERT INTO category_rules(pattern, direction, category, merchant) VALUES (?,?,?,?)",
                 (pattern, direction, category, merchant))
    conn.commit()
    n = recategorize(conn)
    hits = conn.execute("SELECT COUNT(*) FROM transactions WHERE category=? AND category_source!='user'",
                        (category,)).fetchone()[0]
    return {"pattern": pattern, "category": category, "direction": direction, "transactions_rechecked": n,
            "now_in_category": hits}


def set_scope(conn: sqlite3.Connection, account_id: str, scope: str) -> dict:
    if scope not in ("personal", "business"):
        raise ValueError("scope must be personal or business")
    cur = conn.execute("UPDATE accounts SET scope=? WHERE id=?", (scope, account_id))
    if cur.rowcount == 0:
        raise ValueError(f"No account {account_id}")
    conn.commit()
    recategorize(conn)
    return {"account_id": account_id, "scope": scope}
