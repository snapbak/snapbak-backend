"""The worked examples the polish prompt shows the model before each request."""

import json

from snapbak_backend.ai.prompts.polish import message
from snapbak_backend.ai.providers.base import Turn


def example(text: str, prior: str = "", terms: tuple[str, ...] = (), style: str = "chat") -> str:
    request = {"text": text, "prior": prior, "terms": list(terms), "style": style}
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
        example("after the presentation sum of us will grab lunch"),
        reply("After the presentation, some of us will grab lunch."),
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
        example("deploy it to staging scratch that leave it qa is still running"),
        reply("Leave it, QA is still running."),
    ),
    (
        example("the client meeting is on monday no no on tuesday and the deck needs to be ready too"),
        reply("The client meeting is on Tuesday and the deck needs to be ready too."),
    ),
    (
        example("we need two things first budget second hiring", style="docs"),
        reply("We need two things:\n1. Budget\n2. Hiring"),
    ),
    (
        example("the review is at three tomorrow make that four"),
        reply("The review is at 4 tomorrow."),
    ),
    (
        example("First, book the tickets. Second, check the hotel."),
        reply("1. Book the tickets\n2. Check the hotel"),
    ),
    (
        example("get one coffee and two muffins and bring the bill too"),
        reply("Get one coffee and two muffins, and bring the bill too."),
    ),
    (
        example("did meera's parcel come today did the warehouse say anything"),
        reply("Did Meera's parcel come today? Did the warehouse say anything?"),
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
        example("i bought sugar and lentils at the grocer and some flower for the cake"),
        reply("I bought sugar and lentils at the grocer and some flour for the cake."),
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
        example("so what the agenda will be it will be a short review of the numbers"),
        reply("So, what will the agenda be? It will be a short review of the numbers."),
    ),
    (
        example("she found bug in login screen"),
        reply("She found a bug in the login screen."),
    ),
    (
        example("we sat in the garden what a lovely evening"),
        reply("We sat in the garden. What a lovely evening!"),
    ),
    (
        example("when you will land call me"),
        reply("When you land, call me."),
    ),
    (
        example("write down the rent electricity bill and grocery costs for this week"),
        reply("Write down the rent, electricity bill and grocery costs for this week."),
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
        example("reset the app pass word"),
        reply("Reset the app password."),
    ),
    (
        example("tomorrow we submitted the form"),
        reply("Tomorrow we will submit the form."),
    ),
    (
        example("next month we moved to the new house"),
        reply("Next month we will move to the new house."),
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
        example("tell me where the meeting will be?"),
        reply("Tell me where the meeting will be."),
    ),
    (
        example("point one pay the rent point two book the gas", style="docs"),
        reply("1. Pay the rent\n2. Book the gas"),
    ),
    (
        example("the food was very very good"),
        reply("The food was very very good."),
    ),
    (
        example("grandma's house key is in the cupboard no no in the drawer"),
        reply("Grandma's house key is in the drawer."),
    ),
    (
        example("give us a call when you land", terms=("US",)),
        reply("Give us a call when you land."),
    ),
    (
        example("wow what a beautiful view"),
        reply("Wow, what a beautiful view!"),
    ),
    (
        example("kabir send the report today"),
        reply("Kabir, send the report today."),
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
        example("nisha is joining the meeting and rohan is taking notes"),
        reply("Nisha is joining the meeting and Rohan is taking notes."),
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
        example("mom put the lentils in the fridge"),
        reply("Mom put the lentils in the fridge."),
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
        example("the bike's break is loose"),
        reply("The bike's brake is loose."),
    ),
    (
        example("are the 2 tickets confirmed"),
        reply("Are the 2 tickets confirmed?"),
    ),
    (
        example("kavya send the Figma file today", terms=("Figma",)),
        reply("Kavya, send the Figma file today."),
    ),
    (
        example("so what the menu will be it should have soup and rice"),
        reply("So, what will the menu be? It should have soup and rice."),
    ),
    (
        example("print the photos again no no share the photos"),
        reply("Share the photos."),
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
        example("priya looked at the file and said everything is fine"),
        reply("Priya looked at the file and said everything is fine."),
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
    # A lone subject the speech ends on starts nothing and drops; one that answers "who" stays.
    (
        example("They asked me Where do you see yourself, and what do you want to learn? You"),
        reply("They asked me: Where do you see yourself, and what do you want to learn?"),
    ),
    (
        example("who is presenting first? you"),
        reply("Who is presenting first? You."),
    ),
    # A noun that names the items announces them: a bulleted list, a count of them counting nothing off.
    (
        example("my goals for this month are finishing the course. saving some money and then a trip home. that's all"),
        reply("My goals for this month are:\n- Finishing the course\n- Saving some money\n- A trip home\nThat's all."),
    ),
    (
        example("so basically two things are pending, like. the invoice then there is the contract we"),
        reply("So basically two things are pending, like:\n- The invoice\n- There is the contract"),
    ),
]
