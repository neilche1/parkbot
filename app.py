from flask import Flask, request
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse
import openai
from datetime import datetime, timedelta
import os

app = Flask(__name__)

# Twilio Credentials (loaded from environment variables)
TWILIO_SID = os.getenv("TWILIO_SID")
TWILIO_TOKEN = os.getenv("TWILIO_TOKEN")
MESSAGING_SID = os.getenv("MESSAGING_SID", "MGfeeb018ce3174b051057f0c0176d395d")  # Corrected SID
TWILIO_NUMBER = os.getenv("TWILIO_NUMBER", "+19853799364")  # Fallback to hardcoded value if not set

# Owner's Phone Number for Notifications
OWNER_PHONE = os.getenv("OWNER_PHONE", "+15049090355")  # Fallback to hardcoded value if not set

# OpenAI Credentials
OPENAI_KEY = os.getenv("OPENAI_KEY")
openai.api_key = OPENAI_KEY

# Manual Tenant Database (keyed by name and lot number)
# Format: {(name, lot): {"balance": ..., "due_date": ..., "phones": [...]}}
TENANTS = {
    # Oakwood Estates
    ("Clara Ines Wood Lopez", "02"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Melany Sohamy Pineda Maradiaga", "03"): {"balance": "$29.00", "due_date": "1st", "phones": []},
    ("Miguel Tena", "04"): {"balance": "-$75.00", "due_date": "1st", "phones": []},
    ("Janet Smith", "05"): {"balance": "-$40.00", "due_date": "1st", "phones": []},
    ("Diane Kinchen", "06"): {"balance": "$5.00", "due_date": "1st", "phones": []},
    ("Juan A Salazar", "07"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Jose Gustavo Castro", "08"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Marlon Javier Guillen Valladares", "09"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Salvador Lazaro Munoz", "10"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Anni Martinez", "11"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Salvador Lazaro", "13"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Ryan Knighten", "14"): {"balance": "-$5.34", "due_date": "1st", "phones": []},
    ("Drew Jarreau", "15"): {"balance": "-$18.00", "due_date": "1st", "phones": []},
    ("Montreka Stevenson", "16"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Ruth Torres", "17"): {"balance": "$420.00", "due_date": "1st", "phones": []},
    ("Salvador Lazaro", "18"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Guillermo Reyes", "18 A"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Jose Herrera", "19"): {"balance": "-$220.00", "due_date": "1st", "phones": []},
    ("Jesus Rosales", "21"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Yvette Kinchen", "22"): {"balance": "$165.00", "due_date": "1st", "phones": []},
    ("Kenyatti Solomon", "23"): {"balance": "$2005.00", "due_date": "1st", "phones": []},
    ("Reyna Ernestina Cartagena Jovel", "24"): {"balance": "-$75.00", "due_date": "1st", "phones": []},
    ("Brian Holmes", "25"): {"balance": "-$100.00", "due_date": "1st", "phones": []},
    ("Mayolo Perez", "26"): {"balance": "$55.00", "due_date": "1st", "phones": []},
    ("Mayolo Perez Vasquez", "27"): {"balance": "-$95.00", "due_date": "1st", "phones": []},
    ("Jeffrey S Harper", "28"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Courtney Robinson", "29"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Salvador Lazaro", "30"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Consuelo Ulloa", "31"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Maria Covarrubias", "32"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Felipe Cano", "33"): {"balance": "$10.00", "due_date": "1st", "phones": []},
    ("Sindy Paola Rosales Trochez", "34"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Patricia Mayers", "35"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Brandi Hinojoza", "36"): {"balance": "-$5.00", "due_date": "1st", "phones": []},
    ("Sweet Homes 360 LLC", "37"): {"balance": "-$95.00", "due_date": "1st", "phones": []},
    ("Iris Rodriguez", "38"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Ana Rivera-Rodriguez", "39"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Ismael Gutierrez", "40"): {"balance": "-$10.00", "due_date": "1st", "phones": []},
    ("Andres Corral", "41"): {"balance": "$1230.00", "due_date": "1st", "phones": []},
    ("Billy Caballero", "42"): {"balance": "-$20.00", "due_date": "1st", "phones": []},
    ("Clementina Pena De Gutierrez", "43"): {"balance": "-$71.00", "due_date": "1st", "phones": []},
    ("Lewondera Davenport", "44"): {"balance": "$95.00", "due_date": "1st", "phones": []},
    ("Beverly Chiasso", "45"): {"balance": "$62.00", "due_date": "1st", "phones": []},
    ("Robert Luttenbacher", "46"): {"balance": "-$40.00", "due_date": "1st", "phones": []},
    ("Richard Tyler", "Camper A"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    # Shady Nook Park
    ("RUDIS RIVERA", "102 NLop"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("James Hughes", "102 WLoop"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("MANUEL RAMOS", "103 ELp"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JOSE HERNANDEZ", "104 NLop"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("MARIAH MESCALE", "106 NLop"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("MIRIANS FIGUEROA CARDONA", "108 NLop"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Yolanda Escaleras", "108 WLoop"): {"balance": "-$12.00", "due_date": "1st", "phones": []},
    ("gloria Paggoada", "110 WLoop"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("MARIA DELMY YAMILETH", "111 ELp"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("claudia espinoza", "111 Monteg"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("BELLA JULIA FERNANDES", "112 NLop"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("veronica pena reyes", "112 WLoop"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Mario Diaz", "114 NLop"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Sally Solomon", "114 WLoop"): {"balance": "$239.92", "due_date": "1st", "phones": []},
    ("MANUEL AGUILAR", "115 ELp"): {"balance": "-$0.29", "due_date": "1st", "phones": []},
    ("MERLIN LOPEZ OSORIO", "115 Monteg"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("CRISTINO LAZO", "116 NLop"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("MERLIN AGUILAR LIRA", "116 WLoop"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("EVER VALLADARES", "117 Elp"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("WILFRIDO IBAAñEZ", "117 WLoop"): {"balance": "$894.00", "due_date": "1st", "phones": []},
    ("KELIN MARINA BARDALES", "118 Monteg"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("IVAN MARTINEZ", "118 WLoop"): {"balance": "$0.02", "due_date": "1st", "phones": []},
    ("ORALIA ROMERO ORTIZ", "120 Monteg"): {"balance": "$4.79", "due_date": "1st", "phones": []},
    ("Dania Gabriela Smith", "120 WLoop"): {"balance": "-$0.08", "due_date": "1st", "phones": []},
    ("ROSA M ALVARADO ARAGON", "122 Monteg"): {"balance": "$4.79", "due_date": "1st", "phones": []},
    ("NORMA L ACEITUNO HERRERA", "122 WLoop"): {"balance": "-$0.26", "due_date": "1st", "phones": []},
    ("NAHUN E AVILA AMAYA", "123 ELp"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("CARLOS DANIEL MORALES", "124 Monteg"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JESUS MORALES", "125 Monteg"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("MAURA E MEJIA CAMPOS", "127 Monteg"): {"balance": "-$0.74", "due_date": "1st", "phones": []},
    ("cindy espinoza", "128 Monteg"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Arnaldo Suazo", "128 Oswld"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Aminta Rodriguez Gutierrez", "128B Monte"): {"balance": "-$0.28", "due_date": "1st", "phones": []},
    ("JOHN SADOWSKI", "128B-2Mont"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Reynaldo Chinchilla", "129 Monteg"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JESUS MORALEZ", "130 Monteg"): {"balance": "$5.00", "due_date": "1st", "phones": []},
    ("EDER JACIEL ORTIZ LOPEZ", "130 Oswld"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("ANA CARDONA KEVIN GUZMAN", "131 Monteg"): {"balance  "due_date": "1st", "phones": []},
    ("Meryoneyda Osorio", "131 Oswld"): {"balance": "$17244.99", "due_date": "1st", "phones": []},
    ("IRWIN CABRERA", "131Betty"): {"balance": "$920.00", "due_date": "1st", "phones": []},
    ("DORA BRIZUELA", "132 Monteg"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("ELISEO HERNANDEZ", "132 Oswld"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("dolores hernadez", "133 Oswld"): {"balance": "-$0.10", "due_date": "1st", "phones": []},
    ("Zenobia Roussell", "133Betty"): {"balance": "$27333.13", "due_date": "1st", "phones": []},
    ("JOSE FLORES", "134 Monteg"): {"balance": "-$0.51", "due_date": "1st", "phones": []},
    ("MARIBEL MEJIA", "134 Oswld"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("RAFAEL VALENCIA", "135 Monteg"): {"balance": "-$1.21", "due_date": "1st", "phones": []},
    ("IRWIN CABRERA", "135 Oswld"): {"balance": "$925.00", "due_date": "1st", "phones": []},
    ("PEDRO RAYGOZA", "135Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("PABLO A CARDENAS ZAMORA", "136 Oswld"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JORGE BATRES", "137 Oswld"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Moises Gutierrez", "137Betty"): {"balance": "-$3.50", "due_date": "1st", "phones": []},
    ("juana gonzalez", "137bOswld"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("PATRICIA M. RODRIGUEZ", "139 Monteg"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Maynor Perez", "139 Oswld"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("FLAVEO HERNANDEZ", "141 Oswld"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Alejandro Felipe", "142 Oswld"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("jose santamaria", "143Betty"): {"balance": "-$0.59", "due_date": "1st", "phones": []},
    ("JULIO ORELLANA HERRERA", "144 Oswld"): {"balance": "-$29.38", "due_date": "1st", "phones": []},
    ("Alfredo Gonzales", "145 Oswld"): {"balance": "-$0.57", "due_date": "1st", "phones": []},
    ("SANDRA MENDOZA", "145Betty"): {"balance": "-$1.43", "due_date": "1st", "phones": []},
    ("Jose Martinez", "146 Oswld"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Ligia Gutirrez", "147Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("HUGO ALVARADO", "148 Monteg"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("DANIA CHAVER PEREIRA", "149 Monteg"): {"balance": "-$0.61", "due_date": "1st", "phones": []},
    ("Felipe Lucero", "149 Oswld"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Abigail Perez", "150 Oswld"): {"balance": "$9111.02", "due_date": "1st", "phones": []},
    ("jose suazo", "151 Monteg"): {"balance": "$500.00", "due_date": "1st", "phones": []},
    ("Griselda Chavez", "151 WLoop"): {"balance": "$3.90", "due_date": "1st", "phones": []},
    ("CARMINA ROJAS", "152 Monteg"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("MIRIAN BUSTILLO AVILA", "152 Oswld"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("virginia hernandez", "152Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("marina cortez", "153 Monteg"): {"balance": "-$0.85", "due_date": "1st", "phones": []},
    ("Marcial Gomez", "153 Oswld"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("ARTEMIO MORENO", "153Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("jose arizaga ibarra", "154 Oswld"): {"balance": "-$0.64", "due_date": "1st", "phones": []},
    ("YEKSON MARQUEZ", "155 Oswld"): {"balance": "-$5.00", "due_date": "1st", "phones": []},
    ("Erminda Rodriguez-Gutierrez", "156 Oswld"): {"balance": "-$0.20", "due_date": "1st", "phones": []},
    ("AMALIA ARRIAGA", "156Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("ROBER BANEGAS NUNEZ", "157Betty"): {"balance": "-$1.01", "due_date": "1st", "phones": []},
    ("JOSE KATERIN CHAVEZ", "158 Oswld"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("ALBERTO MATA CORONA", "158Betty"): {"balance": "-$4.37", "due_date": "1st", "phones": []},
    ("Maria Ruiz", "159 Oswld"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("OLIMPIA ROSALES CARDENAS", "159Betty"): {"balance": "$3816.35", "due_date": "1st", "phones": []},
    ("MERLIN AGUILAR LIRA", "160Betty"): {"balance": "-$4.74", "due_date": "1st", "phones": []},
    ("Jose Vega", "161 Oswld"): {"balance": "$662.23", "due_date": "1st", "phones": []},
    ("WENDY SANCHEZ", "161Betty"): {"balance": "-$0.47", "due_date": "1st", "phones": []},
    ("Edelmira Lopez", "162Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("ROSA NIETO", "163 Oswld"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("DINA DIXON ALMENDAREZ", "163Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Mario Turcios", "164Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Teresa Barrera", "165Betty"): {"balance": "-$0.44", "due_date": "1st", "phones": []},
    ("SANTOS E LOBO", "166BBetty"): {"balance": "$0.01", "due_date": "1st", "phones": []},
    ("SANTOS E LOBO", "166Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JOSE GARCIA", "167Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JOSE GARCIA", "168Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JOSE GARCIA", "169Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JOSE GARCIA", "170Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JOSE GARCIA", "171Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JOSE GARCIA", "172Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JOSE GARCIA", "173Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JOSE GARCIA", "174Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JOSE GARCIA", "175Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JOSE GARCIA", "176Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JOSE GARCIA", "177Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JOSE GARCIA", "178Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JOSE GARCIA", "179Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JOSE GARCIA", "180Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JOSE GARCIA", "181Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JOSE GARCIA", "182Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JOSE GARCIA", "183Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JOSE GARCIA", "184Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JOSE GARCIA", "185Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JOSE GARCIA", "186Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JOSE GARCIA", "187Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JOSE GARCIA", "188Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JOSE GARCIA", "189Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JOSE GARCIA", "190Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JOSE GARCIA", "191Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JOSE GARCIA", "192Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JOSE GARCIA", "193Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JOSE GARCIA", "194Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JOSE GARCIA", "195Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JOSE GARCIA", "196Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JOSE GARCIA", "197Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JOSE GARCIA", "198Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JOSE GARCIA", "199Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("JOSE GARCIA", "200Betty"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    # Vesta Mobile Home and R.V. Park (Rent corrected: +$75)
    ("Jaime Martinez", "201"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Jose Martinez", "202"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Hector Perla", "203"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Marlon J Guillen", "204"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("George Lannen", "205"): {"balance": "$97.18", "due_date": "1st", "phones": []},
    ("Raquel Bejarano", "206"): {"balance": "-$15.00", "due_date": "1st", "phones": []},
    ("Wayne Ursin", "207"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Oscar Elias Perdomo Espana", "208"): {"balance": "$133.15", "due_date": "1st", "phones": []},
    ("Auner Josue Oliva Madrid", "209"): {"balance": "-$75.00", "due_date": "1st", "phones": []},
    ("Ever Escobar", "210"): {"balance": "-$2.14", "due_date": "1st", "phones": []},
    ("Barbara Johnson", "211"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Melvin G Guillen", "212"): {"balance": "-$5.15", "due_date": "1st", "phones": []},
    ("Rosa N Espana", "213"): {"balance": "-$0.05", "due_date": "1st", "phones": []},
    ("Brenda Guadiana", "214"): {"balance": "-$1.00", "due_date": "1st", "phones": []},
    ("Luis Santana", "215"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Adan Ixtecoc", "216"): {"balance": "-$0.87", "due_date": "1st", "phones": []},
    ("Maria Margarita Hernandez", "217"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Cristal Rojas", "218"): {"balance": "$304.71", "due_date": "1st", "phones": []},
    ("Lorenzo Ramirez", "219"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Alejandra Ochoa", "220"): {"balance": "$120.00", "due_date": "1st", "phones": []},
    ("Refugio Sanchez", "221"): {"balance": "-$100.00", "due_date": "1st", "phones": []},
    ("Jarel Ford", "222"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Angela Rojas", "223"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Marlon Rios", "224"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Anabel Murcia", "225"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Paul Safe", "226"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Yoscelyn Itzel Cruz Miranda", "227"): {"balance": "$126.45", "due_date": "1st", "phones": []},
    ("Ronald Saurage", "228"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Raymond Morris", "229"): {"balance": "$5.00", "due_date": "1st", "phones": []},
    ("Osiris Reyes", "230"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Wendell Winegeart", "231"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Carlos Castaneda", "232"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Donald Preston, Jr.", "233"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Edward Gerald Richardson", "234"): {"balance": "$3129.00", "due_date": "1st", "phones": []},
    ("Charles Parker", "235"): {"balance": "$5.00", "due_date": "1st", "phones": []},
    ("Jorge Garcia", "236"): {"balance": "-$75.00", "due_date": "1st", "phones": []},
    ("Wilda Fontenot", "237"): {"balance": "-$65.00", "due_date": "1st", "phones": []},
    ("Nahum Bautista Gutierrez", "238"): {"balance": "-$175.00", "due_date": "1st", "phones": []},
    ("Ronald Atwood", "239"): {"balance": "-$1.19", "due_date": "1st", "phones": []},
    ("Rony Gonzalez", "240"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Kevin Aguilar", "241"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Joseph Jordan", "242"): {"balance": "$35.00", "due_date": "1st", "phones": []},
    ("Kelly Fabiola Ramos Padilla", "243"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Carlos Morales", "244"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Victor Emilio Duarte Castaneda", "245"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Bartolo Rodriguez", "A"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Juan Paul Aguirre Reyna", "B"): {"balance": "$550.80", "due_date": "1st", "phones": []},
    ("Daina Sancho", "C"): {"balance": "-$0.57", "due_date": "1st", "phones": []},
    # Yorkshire Park I, II
    ("Josefa Martinez", "york 101"): {"balance": "$5.00", "due_date": "1st", "phones": []},
    ("Dominick Hall", "york 102"): {"balance": "-$34.00", "due_date": "1st", "phones": []},
    ("Natalie Barnes", "york 103"): {"balance": "$5.00", "due_date": "1st", "phones": []},
    ("Ashley Williams", "york 104"): {"balance": "-$402.00", "due_date": "1st", "phones": []},
    ("Latasha Corbin", "york 105"): {"balance": "$416.00", "due_date": "1st", "phones": []},
    ("Justin Prevost", "york 106"): {"balance": "-$26.00", "due_date": "1st", "phones": []},
    ("Tyesha Davis", "york 107"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Tyesha Davis", "york 108"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Fantasia Washington", "york 109"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Martine Walker", "york 110"): {"balance": "-$4.00", "due_date": "1st", "phones": []},
    ("Lakeisha Porter", "york 111"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Margaret Provost", "york 112"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Donny Granison", "york 113"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Jeralyne Cook", "york 114"): {"balance": "-$6.00", "due_date": "1st", "phones": []},
    ("Asia Mixon", "york 115"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Shantnell LaFrance", "york 116"): {"balance": "-$23.00", "due_date": "1st", "phones": []},
    ("Joanna Harrell", "york 117"): {"balance": "$7.00", "due_date": "1st", "phones": []},
    ("James Henderson", "york 118"): {"balance": "$669.00", "due_date": "1st", "phones": []},
    ("Wayne Loney", "york 119"): {"balance": "$401.00", "due_date": "1st", "phones": []},
    ("Shamethia Phillips", "york 120"): {"balance": "$402.00", "due_date": "1st", "phones": []},
    ("Tynikqua Howard", "york 202"): {"balance": "-$5.51", "due_date": "1st", "phones": []},
    ("Joie Peters", "york 203"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Juan Moran", "york 206"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("John Porche", "york 207"): {"balance": "-$38.00", "due_date": "1st", "phones": []},
    ("Christopher Gore", "york 208"): {"balance": "$7.00", "due_date": "1st", "phones": []},
    ("Jennifer Stewart", "york 209"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Chalsea Sylve", "york 211"): {"balance": "$377.00", "due_date": "1st", "phones": []},
    ("Betty Hudsen", "york 212"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Delores Bridgewater", "york 214"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Takima Ruiz", "york 215"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Shaquile Stimage", "york 216"): {"balance": "-$5.00", "due_date": "1st", "phones": []},
    ("Selina Bogan", "york 217"): {"balance": "$377.00", "due_date": "1st", "phones": []},
    ("Margaret Provost", "york 218"): {"balance": "-$1.00", "due_date": "1st", "phones": []},
    ("Shadinea Whittington", "york 219"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Michell Scobar", "york 220"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Jasmine Andrews", "york 221"): {"balance": "$335.00", "due_date": "1st", "phones": []},
    ("Eriel Long-McGowan", "york 222"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Liliana Hernandez", "york 223"): {"balance": "-$0.70", "due_date": "1st", "phones": []},
    ("Emilio Murillo", "york 225"): {"balance": "-$7.00", "due_date": "1st", "phones": []},
    ("Jonie Darmas", "york 226"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Ayrial Evans", "york 227"): {"balance": "$156.00", "due_date": "1st", "phones": []},
    ("Marquilla Lipkin", "york 228"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Latoya Porter", "york 229"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Jennifer Young", "york 230"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Hope Miller", "york 231"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Harleatricia Robinson", "york 232"): {"balance": "-$6.00", "due_date": "1st", "phones": []},
    ("Brittany Jones", "york 233"): {"balance": "-$10.00", "due_date": "1st", "phones": []},
    ("Quantesha Hooker", "york 234"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Brittany Hart", "york 235"): {"balance": "-$1.37", "due_date": "1st", "phones": []},
    ("Lakeisha Porter", "york 237"): {"balance": "$5.00", "due_date": "1st", "phones": []},
    ("Tykira Lewis", "york 238"): {"balance": "$804.00", "due_date": "1st", "phones": []},
    ("Authencia Lee", "york 239"): {"balance": "-$1.00", "due_date": "1st", "phones": []},
    ("Fatima Lopez", "york 240"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Whitney Williams", "york 241"): {"balance": "-$8.00", "due_date": "1st", "phones": []},
    ("Gloria Wilson", "york 242"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Dominique Lee", "york 243"): {"balance": "-$18.00", "due_date": "1st", "phones": []},
    ("Charles Jenkins", "york 244"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Reginald Stimage", "york 245"): {"balance": "-$1.00", "due_date": "1st", "phones": []},
    ("Patricia Brown", "york 246"): {"balance": "$341.00", "due_date": "1st", "phones": []},
    ("Clarice Webb", "york 247"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Ciera Nichols", "york 248"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Brialle Sellers", "york 249"): {"balance": "$377.00", "due_date": "1st", "phones": []},
    ("Shavonna Baskin", "york 250"): {"balance": "-$3.00", "due_date": "1st", "phones": []},
    ("Jennifer Steward", "york 251"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Chtistina Austin", "york 252"): {"balance": "-$10.00", "due_date": "1st", "phones": []},
    ("Brittany Jones", "york 253"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Zaneitha Collier", "york 254"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Antwonitian Ray", "york 255"): {"balance": "-$3.00", "due_date": "1st", "phones": []},
    ("Kimberly Collins", "york 256"): {"balance": "$0.00", "due_date": "1st", "phones": []},
    ("Crystal Jackson", "york 258"): {"balance": "-$5.00", "due_date": "1st", "phones": []},
    ("Sasha Davis", "york 261"): {"balance": "$0.00", "due_date": "1st", "phones": []},
}

# Phone-to-Tenant Mapping (for recognized numbers)
PHONE_TO_TENANT = {
    # Waiting for phone numbers (if provided)
}

# Rent Rule
RENT_DUE_DAY = 1  # Due on the 1st of each month
LATE_FEE_PER_DAY = 5  # $5 per day after the 5th
LATE_FEE_START_DAY = 5  # Late fees start after the 5th

MAINTENANCE_REQUESTS = []
CALL_LOGS = []
PENDING_IDENTIFICATION = {}

def get_tenant_data(phone_number):
    tenant_key = PHONE_TO_TENANT.get(phone_number)
    if tenant_key:
        return TENANTS.get(tenant_key, {"name": "Unknown", "balance": "N/A", "due_date": "N/A"})
    return None

def identify_tenant(name, lot):
    tenant_key = (name.strip(), lot.strip())
    if tenant_key in TENANTS:
        return tenant_key
    return None

def get_ai_response(user_input, tenant_data):
    prompt = f"Act as a mobile home park manager. Tenant data: {tenant_data}. Query: {user_input}"
    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a mobile home park manager assisting tenants."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=50
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Error in get_ai_response: {str(e)}")
        return "I’m sorry, I couldn’t process your request at this time. Please try again later or contact the park manager directly."

def send_sms(to_number, message):
    print(f"Sending SMS to {to_number}: {message}")
    print(f"TWILIO_SID: {TWILIO_SID}")
    print(f"TWILIO_TOKEN: {TWILIO_TOKEN}")
    try:
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        print("Twilio client initialized")
        # Use messaging_service_sid if available, otherwise fall back to from_ number
        if MESSAGING_SID:
            response = client.messages.create(
                messaging_service_sid=MESSAGING_SID,
                body=message,
                to=to_number
            )
        else:
            response = client.messages.create(
                from_=TWILIO_NUMBER,
                body=message,
                to=to_number
            )
        print(f"SMS sent successfully: {response.sid}")
    except Exception as e:
        print(f"Error sending SMS: {str(e)}")
        raise

# Homepage route to avoid 404 error
@app.route("/", methods=["GET"])
def home():
    return "ParkBot is running! Use Twilio to interact via SMS or voice."

# Keep-alive endpoint (optional, since you're on a paid plan)
@app.route("/keep_alive", methods=["GET"])
def keep_alive():
    return "App is awake!"

@app.route("/sms", methods=["POST"])
def sms_reply():
    print("Received SMS request")
    from_number = request.values.get("From")
    message = request.values.get("Body").strip()
    print(f"From: {from_number}, Message: {message}")

    tenant_data = get_tenant_data(from_number)
    if tenant_data:
        message_lower = message.lower()
        if "maintenance" in message_lower or "fix" in message_lower or "broken" in message_lower:
            # Log the maintenance request
            tenant_key = PHONE_TO_TENANT[from_number]
            tenant_name, tenant_lot = tenant_key
            MAINTENANCE_REQUESTS.append({
                "tenant_phone": from_number,
                "tenant_name": tenant_name,
                "tenant_lot": tenant_lot,
                "issue": message
            })
            # Notify the owner
            owner_message = f"Maintenance request from {tenant_name}, Unit {tenant_lot}: {message}"
            send_sms(OWNER_PHONE, owner_message)
            # Reply to the tenant
            reply = "Your maintenance request has been logged and forwarded to the owner. You’ll be contacted soon."
        else:
            reply = get_ai_response(message, tenant_data)
        send_sms(from_number, reply)
        return "OK"

    if from_number in PENDING_IDENTIFICATION:
        try:
            name, lot = message.split(",", 1)
            tenant_key = identify_tenant(name, lot)
            if tenant_key:
                PHONE_TO_TENANT[from_number] = tenant_key
                TENANTS[tenant_key]["phones"].append(from_number)
                del PENDING_IDENTIFICATION[from_number]
                reply = "Thank you! I’ve identified you. How can I assist you today?"
            else:
                reply = "Sorry, I couldn’t find a tenant with that name and lot number. Please try again (e.g., John Doe, Lot 5)."
        except ValueError:
            reply = "Please provide your name and lot number in the format: Name, Lot (e.g., John Doe, Lot 5)."
        send_sms(from_number, reply)
        return "OK"

    PENDING_IDENTIFICATION[from_number] = "awaiting_name_lot"
    send_sms(from_number, "I don’t recognize this number. Please provide your name and lot number (e.g., John Doe, Lot 5).")
    return "OK"

@app.route("/voice", methods=["POST"])
def voice_reply():
    print("Received voice request")
    from_number = request.values.get("From")
    CALL_LOGS.append({
        "phone_number": from_number,
        "call_type": "incoming",
        "timestamp": datetime.now().isoformat(),
        "notes": "Incoming call handled by ParkBot"
    })
    resp = VoiceResponse()
    resp.say("Hello, this is ParkBot. I don’t recognize this number. Please text me your name and lot number.")
    return str(resp)

def send_rent_reminders():
    print("Starting send_rent_reminders")
    today = datetime.now()
    day = today.day
    last_day_of_prev_month = (today.replace(day=1) - timedelta(days=1)).day

    for tenant_key, tenant in TENANTS.items():
        print(f"Processing tenant: {tenant_key}")
        try:
            # Strip any non-numeric suffix (e.g., "st", "nd", "rd", "th") from due_date
            due_day_str = tenant["due_date"].split()[0]  # e.g., "1st"
            due_day = int(''.join(filter(str.isdigit, due_day_str)))  # e.g., 1
            tenant_phones = tenant["phones"]

            if day == last_day_of_prev_month:
                for phone in tenant_phones:
                    print(f"Sending reminder to {phone}")
                    send_sms(phone, f"Reminder: Your rent of {tenant['balance']} is due tomorrow on the {tenant['due_date']}.")
            elif day > LATE_FEE_START_DAY:
                days_late = day - LATE_FEE_START_DAY
                late_fee = days_late * LATE_FEE_PER_DAY
                for phone in tenant_phones:
                    print(f"Sending late fee notice to {phone}")
                    send_sms(phone, f"Your rent of {tenant['balance']} is past due. A late fee of ${late_fee} has been applied.")
        except Exception as e:
            print(f"Error processing tenant {tenant_key}: {str(e)}")
    print("Finished send_rent_reminders")

@app.route("/send_rent_reminders", methods=["GET"])
def trigger_rent_reminders():
    print("Triggering rent reminders")
    try:
        send_rent_reminders()
        print("Rent reminders sent successfully")
        return "Rent reminders sent!"
    except Exception as e:
        print(f"Error in send_rent_reminders: {str(e)}")
        return f"Error: {str(e)}", 500

if __name__ == "__main__":
    app.run(debug=True)