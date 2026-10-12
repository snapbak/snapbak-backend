"""The worked examples the polish prompt shows the model before each request."""

import json

from snapbak_backend.ai.prompts.polish import message
from snapbak_backend.ai.providers.base import Turn


def example(text: str, prior: str = "", terms: tuple[str, ...] = (), style: str = "chat", language: str = "en") -> str:
    request = {"text": text, "prior": prior, "terms": list(terms), "style": style, "language": language}
    return message(request)


def reply(text: str, revision: str | None = None) -> str:
    return json.dumps({"revision": revision, "text": text}, ensure_ascii=False)


EXAMPLES: list[Turn] = [
    (
        example("we need three things first the api second the ui third the docs", style="docs"),
        reply("We need three things:\n1. The API\n2. The UI\n3. The docs"),
    ),
    (
        example("do one thing check the logs then restart the worker"),
        reply("Do one thing: check the logs, then restart the worker."),
    ),
    (
        example(
            "the invoice is twenty five dollars send it to john at procol dot in by march third",
            style="email",
        ),
        reply("The invoice is $25. Send it to john@procol.in by March 3."),
    ),
    (
        example("send the invoice to northwind no to Tidewell", terms=("Tidewell",)),
        reply("Send the invoice to Tidewell."),
    ),
    (
        example("the soup needs more taste before we serve it"),
        reply("The soup needs more taste before we serve it."),
    ),
    (
        example("we need a nimble plan for the nimbl rollout", terms=("Nimbl",)),
        reply("We need a nimble plan for the Nimbl rollout."),
    ),
    (
        example("move the old files to flow stack before noon", terms=("Flowstak",)),
        reply("Move the old files to Flowstak before noon."),
    ),
    (
        example("some notes for the offsite we leave friday the venue has a pool we return sunday"),
        reply("Some notes for the offsite:\n- We leave Friday\n- The venue has a pool\n- We return Sunday"),
    ),
    (
        example(
            "I need these in a numbered list. One, book the cab. Two, pack the charger. And three, call the hotel. "
            "Thanks."
        ),
        reply("I need these in a numbered list:\n1. Book the cab\n2. Pack the charger\n3. Call the hotel\nThanks."),
    ),
    (
        example("the vendor want the contract by thursday can you check if the redlines is done"),
        reply("The vendor wants the contract by Thursday. Can you check if the redlines are done?"),
    ),
    (
        example("presentation ke baad sub log lunch karenge", language="hinglish"),
        reply("Presentation ke baad sab log lunch karenge."),
    ),
    (
        example("रिपोर्ट फोल्डर मैं रख दी है", language="hi"),
        reply("रिपोर्ट फोल्डर में रख दी है।"),
    ),
    (
        example("no wait make it thursday", prior="Let's meet on Friday at noon."),
        reply("", revision="Let's meet on Thursday at noon."),
    ),
    (
        example("No, don't send it yet.", prior="We should send this to the frontend team."),
        reply("Don't send it yet.", revision=""),
    ),
    (
        example("second the ui third the docs", prior="We need three things:\n1. The API", style="docs"),
        reply("2. The UI\n3. The docs"),
    ),
    (
        example("no problem i mean it the demo actually went well"),
        reply("No problem, I mean it. The demo actually went well."),
    ),
    (
        example("put the logo on the left i mean the right side of the header"),
        reply("Put the logo on the right side of the header."),
    ),
    (
        example("i booked the window seat rather the aisle seat for the flight"),
        reply("I booked the aisle seat for the flight."),
    ),
    (
        example("staging pe deploy karo matlab rehne do abhi QA chal raha hai", language="hinglish"),
        reply("Rehne do, abhi QA chal raha hai."),
    ),
    (
        example(
            "kal client ke saath meeting hai nahi nahi parso hai aur deck bhi ready karna hai",
            language="hinglish",
        ),
        reply("Parso client ke saath meeting hai aur deck bhi ready karna hai."),
    ),
    (
        example("do cheezein chahiye pehla budget doosra hiring", style="docs", language="hinglish"),
        reply("Do cheezein chahiye:\n1. Budget\n2. Hiring"),
    ),
    (
        example("कल तीन बजे मीटिंग है मतलब चार बजे", language="hi"),
        reply("कल 4 बजे मीटिंग है।"),
    ),
    (
        example("पहला, टिकट बुक करो। दूसरा, होटल देखो।", language="hi"),
        reply("1. टिकट बुक करो\n2. होटल देखो"),
    ),
    (
        example("ek chai aur do samose lao aur bill bhi le aana", language="hinglish"),
        reply("Ek chai aur do samose lao, aur bill bhi le aana."),
    ),
    (
        example("meera ka parcel aaj aaya kya warehouse wale ne kuch bola", language="hinglish"),
        reply("Meera ka parcel aaj aaya kya? Warehouse wale ne kuch bola?"),
    ),
    (
        example("we packed the boxes then we called the movers"),
        reply("We packed the boxes. Then we called the movers."),
    ),
    (
        example("i no the vendor will bring to extra chairs"),
        reply("I know the vendor will bring two extra chairs."),
    ),
    (
        example("the fan is running fine now just check if the fan is ruining at night"),
        reply("The fan is running fine now. Just check if the fan is running at night."),
    ),
    (
        example("steal frame bikes"),
        reply("Steel frame bikes."),
    ),
    (
        example("joe log late aaye unka naam register may likho", language="hinglish"),
        reply("Jo log late aaye, unka naam register mein likho."),
    ),
    (
        example("मैंने किराने की दुकान से चीनी और दल खरीदी", language="hi"),
        reply("मैंने किराने की दुकान से चीनी और दाल खरीदी।"),
    ),
    (
        example("sorry i am late the cab actually came early but i missed it"),
        reply("Sorry, I am late. The cab actually came early, but I missed it."),
    ),
    (
        example("so where the files will be they are in the shared drive"),
        reply("So, where will the files be? They are in the shared drive."),
    ),
    (
        example("I found the charger. Is in the top drawer."),
        reply("I found the charger. It's in the top drawer."),
    ),
    (
        example("okay ravi invoice sent"),
        reply("Okay, Ravi, invoice sent."),
    ),
    (
        example("please zoom in on the chart before our zoom call", terms=("Zoom",)),
        reply("Please zoom in on the chart before our Zoom call."),
    ),
    (
        example("i'll have an apple tart and get him a napple tart too"),
        reply("I'll have an apple tart and get him an apple tart too."),
    ),
    (
        example("the app is loading now see if the nap is loading on her phone"),
        reply("The app is loading now. See if the app is loading on her phone."),
    ),
    (
        example("the cardiologist ordered an eco of her heart"),
        reply("The cardiologist ordered an echo of her heart."),
    ),
    (
        example("we built a simple stating machine for the checkout"),
        reply("We built a simple state machine for the checkout."),
    ),
    (
        example("किसान ने खेत में गेहूं के बीच बोए", language="hi"),
        reply("किसान ने खेत में गेहूं के बीज बोए।"),
    ),
    (
        example("so what the agenda will be it will be a short review of the numbers"),
        reply("So, what will the agenda be? It will be a short review of the numbers."),
    ),
    (
        example("she found bug in login screen"),
        reply("She found a bug in the login screen."),
    ),
    (
        example("garden me baith gaye kya mausam hai", language="hinglish"),
        reply("Garden me baith gaye. Kya mausam hai!"),
    ),
    (
        example("when you will land mujhe call kar dena", language="hinglish"),
        reply("When you land, mujhe call kar dena."),
    ),
    (
        example("mene kal uska msg dekha tha pr reply nhi kiya", language="hinglish"),
        reply("Mene kal uska msg dekha tha pr reply nhi kiya."),
    ),
    (
        example("मै कल रिपोर्ट भेजूंगा", language="hi"),
        reply("मैं कल रिपोर्ट भेजूंगा।"),
    ),
    (
        example("payment dhai lakh ka hai aur sava nau baje tak bhejna hai", language="hinglish"),
        reply("Payment ₹2.5 lakh ka hai aur 9:15 baje tak bhejna hai."),
    ),
    (
        example("इस हफ्ते किराया बिजली बिल और राशन का खर्च लिखो", language="hi"),
        reply("इस हफ्ते किराया, बिजली बिल और राशन का खर्च लिखो।"),
    ),
    (
        example("a few questions first is it ready second can we ship it today that's all"),
        reply("A few questions:\n1. Is it ready?\n2. Can we ship it today?\nThat's all."),
    ),
    (
        example("i mean blue", prior="Order the red mug for Sam."),
        reply("", revision="Order the blue mug for Sam."),
    ),
    (
        example("thanks for the swift reply i will add it to notion", terms=("Notion",)),
        reply("Thanks for the swift reply. I will add it to Notion."),
    ),
    (
        example("tell me a joke about cricket"),
        reply("Tell me a joke about cricket."),
    ),
    (
        example("the oven is heating up now just checking if olive is heating up"),
        reply("The oven is heating up now. Just checking if the oven is heating up."),
    ),
    (
        example("the knew acme invoice is late", terms=("Acme",)),
        reply("The new Acme invoice is late."),
    ),
    (
        example("plain fare"),
        reply("Plane fare."),
    ),
    (
        example("push each job onto a message cue first"),
        reply("Push each job onto a message queue first."),
    ),
    (
        example("chai or biscuit dono le aao", language="hinglish"),
        reply("Chai aur biscuit dono le aao."),
    ),
    (
        example("amit buy ki shaadi agle mahine hai", language="hinglish"),
        reply("Amit bhai ki shaadi agle mahine hai."),
    ),
    (
        example("been a ticket entry nahi milegi", language="hinglish"),
        reply("Bina ticket entry nahi milegi."),
    ),
    (
        example("ऐप का पास वर्ड रीसेट करो", language="hi"),
        reply("ऐप का पासवर्ड रीसेट करो।"),
    ),
    (
        example("tomorrow we submitted the form"),
        reply("Tomorrow we will submit the form."),
    ),
    (
        example("agle mahine hum naye ghar me shift hue", language="hinglish"),
        reply("Agle mahine hum naye ghar me shift honge."),
    ),
    (
        example("अगले साल वो कॉलेज गया था", language="hi"),
        reply("अगले साल वो कॉलेज जाएगा।"),
    ),
    (
        example("didi kal delhi se aayega", language="hinglish"),
        reply("Didi kal Delhi se aayegi."),
    ),
    (
        example("they were check the logs when it crashed"),
        reply("They were checking the logs when it crashed."),
    ),
    (
        example("let's do one thing water the plants then we'll lock the shed. then we can leave for the station"),
        reply("Let's do one thing:\n- Water the plants\n- We'll lock the shed\n- We can leave for the station"),
    ),
    (
        example("mujhe batao meeting kahan hogi?", language="hinglish"),
        reply("Mujhe batao meeting kahan hogi."),
    ),
    (
        example("pehla point rent bharna doosra point gas book karni", style="docs", language="hinglish"),
        reply("1. Rent bharna\n2. Gas book karni"),
    ),
    (
        example("papa ne kaha nahi nahi gaadi mat lena", language="hinglish"),
        reply("Papa ne kaha, nahi nahi, gaadi mat lena."),
    ),
    (
        example("budget tight hai matlab abhi hiring nahi hogi", language="hinglish"),
        reply("Budget tight hai, matlab abhi hiring nahi hogi."),
    ),
    (
        example("the food was very very good"),
        reply("The food was very very good."),
    ),
    (
        example("dadi ke ghar ki chabi almari me hai nahi nahi drawer me hai", language="hinglish"),
        reply("Dadi ke ghar ki chabi drawer me hai."),
    ),
    (
        example("give us a call when you land", terms=("US",)),
        reply("Give us a call when you land."),
    ),
    (
        example("wah kitna sundar view hai", language="hinglish"),
        reply("Wah, kitna sundar view hai!"),
    ),
    (
        example("कबीर रिपोर्ट आज भेज दो", language="hi"),
        reply("कबीर, रिपोर्ट आज भेज दो।"),
    ),
    (
        example("pack these for the beach towels sunscreen two hats the bus leaves at nine"),
        reply("Pack these for the beach:\n- Towels\n- Sunscreen\n- Two hats\nThe bus leaves at 9."),
    ),
    (
        example("it was you know a long week"),
        reply("It was, you know, a long week."),
    ),
    (
        example("my manager told that the review moved to friday"),
        reply("My manager said that the review moved to Friday."),
    ),
    (
        example("once the parcel will arrive we can leave"),
        reply("Once the parcel arrives, we can leave."),
    ),
    (
        example("nisha meeting me aa rahi hai aur rohan notes bana raha hai", language="hinglish"),
        reply("Nisha meeting me aa rahi hai aur Rohan notes bana raha hai."),
    ),
    (
        example("the api is responding again now just a quick check to see if happy is rebounding"),
        reply("The API is responding again now. Just a quick check to see if the API is responding."),
    ),
    (
        example("here are a few things about our trip we leave on friday we fly to goa and we stay near the beach"),
        reply(
            "Here are a few things about our trip:\n- We leave on Friday\n- We fly to Goa and we stay near the beach"
        ),
    ),
    (
        example("Here are a few things about my week. I start a new course. I visit my parents on Sunday."),
        reply("Here are a few things about my week:\n- I start a new course\n- I visit my parents on Sunday"),
    ),
    (
        example("next month we move to pune and then we unpack"),
        reply("Next month we move to Pune, and then we unpack."),
    ),
    (
        example("माँ ने दाल को फ्रिज में रख दिया", language="hi"),
        reply("माँ ने दाल को फ्रिज में रख दिया।"),
    ),
    (
        example("the baker wiped the pasture shelf and stacked the croissants"),
        reply("The baker wiped the pastry shelf and stacked the croissants."),
    ),
    (
        example("hole wheat bread"),
        reply("Whole wheat bread."),
    ),
    (
        example("cycle ka break dheela hai", language="hinglish"),
        reply("Cycle ka brake dheela hai."),
    ),
    (
        example("2 tickets confirm ho gaye kya", language="hinglish"),
        reply("2 tickets confirm ho gaye kya?"),
    ),
    (
        example("kavya Figma file aaj bhej do", terms=("Figma",), language="hinglish"),
        reply("Kavya, Figma file aaj bhej do."),
    ),
    (
        example("so what the menu will be it should have soup and rice"),
        reply("So, what will the menu be? It should have soup and rice."),
    ),
    (
        example("photos ko phir se print karo nahi nahi share karo", language="hinglish"),
        reply("Photos ko share karo."),
    ),
    (
        example("sorry the baker that the cake is ready", prior="Text the florist"),
        reply("", revision="Text the baker that the cake is ready."),
    ),
    (
        example("tom cooks lisa cleans"),
        reply("Tom cooks. Lisa cleans."),
    ),
    (
        example("mom said call me when you reach"),
        reply("Mom said call me when you reach."),
    ),
    (
        example("knee is better no swelling just stiffness in the morning"),
        reply("Knee is better. No swelling, just stiffness in the morning."),
    ),
    # Round b6: a piece that opens with a cue and restates the prior's last words revises it, without the cue.
    (
        example("i mean at six thirty because the hall opens late", prior="The rehearsal starts on Thursday at five."),
        reply("", revision="The rehearsal starts on Thursday at 6:30 because the hall opens late."),
    ),
    (
        example(
            "matlab friday ko office party rakhte hain sab log saat baje aa jaana",
            prior="Is hafte Thursday ko office party rakhte hain. Sab log 6 baje aa jaana.",
            language="hinglish",
        ),
        reply("", revision="Is hafte Friday ko office party rakhte hain. Sab log 7 baje aa jaana."),
    ),
    # With "ने" and a "कि" clause the verb stays masculine, whoever speaks; a right sentence comes back as said.
    (
        example("प्रिया ने फाइल देखी और बोला कि सब ठीक है", language="hi"),
        reply("प्रिया ने फाइल देखी और बोला कि सब ठीक है।"),
    ),
    # A noun that can't do what the words around it say is its sound-alike, also in a few words.
    (
        example("the clearance sail starts friday"),
        reply("The clearance sale starts Friday."),
    ),
    (
        example("ravi here the build is green and the tests pass"),
        reply("Ravi here. The build is green and the tests pass."),
    ),
]
