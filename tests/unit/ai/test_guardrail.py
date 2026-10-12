import gzip
import json
import unicodedata
from pathlib import Path

import pytest
from gen_corpus_v2 import gold_refused

from snapbak_backend.ai.guardrail.corrections import last_cue
from snapbak_backend.ai.guardrail.judge import Polished, Reason, Reject, changed, check, judge, tidy, verdict
from snapbak_backend.ai.guardrail.tokens import align, tokens

CORPUS = Path(__file__).parents[3] / "bench" / "cleanup"

ACCEPTED = [
    # A digit Whisper wrote may become its word, as the prompt asks for numbers up to ten (founder, 11 Oct).
    (
        "Ishaan do 1 thing. Get in touch with Aran directly for this and whatever he says then call archana.",
        "Ishaan, do one thing. Get in touch with Aran directly for this, and whatever he says, then call Archana.",
    ),
    ("we have 2 options", "We have two options."),
    ("1 kaam karo, report bhejo", "Ek kaam karo, report bhejo."),
    ("mere paas 3 ideas hain", "Mere paas teen ideas hain."),
    ("1 काम करो, रिपोर्ट भेजो", "एक काम करो, रिपोर्ट भेजो।"),
    ("हमारे पास 2 रास्ते हैं", "हमारे पास दो रास्ते हैं।"),
    # A stutter: a word said twice in a row may lose one copy (founder, 11 Oct).
    ("It's it's finished finished.", "It's finished."),
    ("we we shipped shipped it", "We shipped it."),
    ("the the build is green", "The build is green."),
    ("kal kal meeting hai", "Kal meeting hai."),
    ("report bhej bhej diya", "Report bhej diya."),
    ("मैंने मैंने रिपोर्ट भेज दी", "मैंने रिपोर्ट भेज दी।"),
    ("हम कल कल मिलेंगे", "हम कल मिलेंगे।"),
    (
        "hey team i pushed the fix for the login issue can someone test it",
        "Hey team, I pushed the fix for the login issue. Can someone test it?",
    ),
    (
        "we need three things first the api second the ui third the docs",
        "We need three things:\n1. The API\n2. The UI\n3. The docs",
    ),
    ("for the trip bullet towels bullet a charger", "For the trip:\n- Towels\n- A charger"),
    ("it costs twenty five dollars", "It costs $25."),
    ("we ship on march third at nine am", "We ship on March 3 at 9 am."),
    ("mail john at procol dot in today", "Mail john@procol.in today."),
    (
        "the endpoint is slash users slash id and it returns json",
        "The endpoint is /users/:id and it returns JSON.",
    ),
    (
        "hey team comma i pushed the fix full stop can someone test it question mark",
        "Hey team, I pushed the fix. Can someone test it?",
    ),
    ("I has already send the document yesterday.", "I had already sent the document yesterday."),
    ("she don't know that the files was moved", "She doesn't know that the files were moved."),
    ("they wasn't ready", "They weren't ready."),
    ("I has already send the document", "I had already sent the document."),
    ("usne report bheja tha", "Usne report bheji thi."),
    ("वह रिपोर्ट भेजता था", "वह रिपोर्ट भेजती थी।"),
    ("ship it please", "Ship it."),
    ("sure i can take the review", "Sure, I can take the review."),
    ("deploy on tuesday actually wednesday", "Deploy on Wednesday."),
    ("we celebrate at our place wait at grandmas place", "We celebrate at Grandma's place."),
    ("Send this to Jam Co. No, send this to GMMCO.", "Send this to GMMCO."),
    ("send this to jam co no send this to GMMCO", "Send this to GMMCO."),
    (
        "kal client ko call karna hai aur report bhejni hai",
        "Kal client ko call karna hai aur report bhejni hai.",
    ),
    ("kal milte hain nahi nahi parso milte hain", "Parso milte hain."),
    ("teen baje meeting hai", "3 baje meeting hai."),
    ("कल रिपोर्ट भेजनी है", "कल रिपोर्ट भेजनी है।"),
    ("कल मिलते हैं नहीं नहीं परसों मिलते हैं", "परसों मिलते हैं।"),
    ("मुझे कल तीन बजे कॉल करना", "मुझे कल 3 बजे कॉल करना है।"),
    ("kevin seen the error", "Kevin saw the error."),
    ("i already send it and they pay it", "I already sent it and they paid it."),
    ("my notes goes missing", "My notes go missing."),
    ("we has a idea", "We have an idea."),
    ("he were late, they was early", "He was late, they were early."),
    ("pehla budget doosra hiring", "1. Budget\n2. Hiring"),
    ("do cheezein pehla budget doosra hiring", "Do cheezein:\n1. Budget\n2. Hiring"),
    ("पहला बजट दूसरा भर्ती", "1. बजट\n2. भर्ती"),
    ("firstly the budget secondly the hiring", "1. The budget\n2. The hiring"),
    ("order pizza for lunch. actually let's get sushi", "Let's get sushi."),
    ("Book the flight to Delhi tomorrow. Scratch that, call Priya.", "Call Priya."),
    ("Kal Delhi ki flight book karo. Nahi nahi, Priya ko call karo.", "Priya ko call karo."),
    ("We should send this to the frontend team. No, don't send it yet.", "Don't send it yet."),
    ("please ship the build to staging today thanks", "Ship the build to staging today."),
    ("the api path is slash v one slash notes slash note id", "The API path is /v1/notes/:noteId"),
    ("unit seven b is on the fourth floor", "Unit 7B is on the 4th floor."),
    ("we need a one hundred rupee budget", "We need a ₹100 budget."),
    ("ill keep it so i dont lose it", "I'll keep it so I don't lose it."),
    ("the oncall flagged it", "The on-call flagged it."),
    ("i can't go but i will not stay", "I cannot go, but I will not stay."),
    ("the folder is slash c o r p", "The folder is /corp."),
    ("it lives under slash users slash tmp let's change that slash users slash prod", "It lives under /users/prod."),
    ("the no fault clause is missing", "The no-fault clause is missing."),
    ("save it to session underscore a17", "Save it to session_a17."),
    ("send report today", "Send the report today."),
    ("the deposit is twelve thousand two hundred dollars", "The deposit is $12,200."),
    ("call me at nine one one or at ten forty five", "Call me at 911 or at 10:45."),
    ("i buyed a laptop", "I bought a laptop."),
    ("we thinked so", "We thought so."),
    ("he has did it", "He has done it."),
    ("the mouses are broken", "The mice are broken."),
    ("i stop the build yesterday", "I stopped the build yesterday."),
    ("वह गया था", "वह गई थी।"),
    ("वह लड़की आया था", "वह लड़की आई थी।"),
    ("लड़कियां आया", "लड़कियां आईं।"),
    ("ok. fifty five dollars", "Ok. $55."),
    ("fifty five dollars", "$55."),
    ("बुलेट तौलिया बुलेट चार्जर", "- तौलिया\n- चार्जर"),
    ("report bhejo nahin, deck bhejo", "Deck bhejo."),
    ("call at ten after three", "Call at 3:10."),
    ("dial 555-0147 now", "Dial 555-0147 now."),
    ("मीटिंग सोमवार को है बल्कि मंगलवार को है", "मीटिंग मंगलवार को है।"),
    ("meeting monday ko hai balki tuesday ko hai", "Meeting Tuesday ko hai."),
    ("जयराम का पेमेंट रन रविवार को नहीं बल्कि शनिवार रात आठ बजे होगा", "जयराम का पेमेंट रन शनिवार रात 8 बजे होगा।"),
    ("बैठक के बाद रोहित से बात करना, बल्कि रौनक से बात करना", "बैठक के बाद रौनक से बात करना।"),
    ("कृपया GST फाइलिंग दस तारीख को, sorry ग्यारह तारीख को फाइनल करना", "कृपया GST फाइलिंग 11 तारीख को फाइनल करना।"),
    ("ek tab night me dena scratch that do tab night me dena", "2 tab night me dena."),
    ("मीटिंग बारह बजे है", "मीटिंग 12 बजे है।"),
    ("baarah mahine ka runway", "12 mahine ka runway."),
    ("runway barah mahine ka hai", "Runway 12 mahine ka hai."),
    ("pachees hazaar rupaye", "₹25,000."),
    ("dus baje", "10 baje."),
    ("बारह महीने का रनवे", "12 महीने का रनवे।"),
    ("पांच बजे कॉल है और छब्बीस मिनट बाद अपडेट", "5 बजे कॉल है और 26 मिनट बाद अपडेट।"),
    ("हमारा रनवे बारह महीने का है लेकिन कैश बर्न तेज है", "हमारा रनवे 12 महीने का है लेकिन कैश बर्न तेज है।"),
    ("तीस प्रतिशत यूजर्स ने फीचर इस्तेमाल किया", "30% यूजर्स ने फीचर इस्तेमाल किया।"),
    ("कुल बिल एक लाख बीस हजार आएगा", "कुल बिल ₹1,20,000 आएगा।"),
    ("पांच हज़ार रुपये भेज दो", "5,000 रुपये भेज दो।"),
    ("saat baje paanch log aayenge", "7 baje 5 log aayenge."),
    ("रिफंड के लिए बिल्स एट प्रोकोल डॉट इन पर इनवॉइस भेजें", "रिफंड के लिए bills@procol.in पर इनवॉइस भेजें।"),
    ("डॉक्युमेंट अपलोड करो drive डॉट google डॉट com स्लैश ops", "डॉक्युमेंट अपलोड करो drive.google.com/ops।"),
    ("पेमेंट पेज पर जाइए डब्ल्यू डब्ल्यू डब्ल्यू डॉट पेमेंट डॉट कॉम स्लैश इनवॉइस", "पेमेंट पेज पर जाइए www.payment.com/invoice।"),
    ("लिंक एच टी टी पी एस कोलन स्लैश स्लैश हेल्प डॉट इन स्लैश एच ई एल पी है", "लिंक https://help.in/help है।"),
    ("रिव्यू साढ़े दस बजे होगा", "रिव्यू 10:30 बजे होगा।"),
    ("pick up the kids by half past three", "Pick up the kids by 3:30."),
    (
        "agenda bullet one simplify onboarding bullet two keep dark mode",
        "Agenda:\n- Simplify onboarding\n- Keep dark mode",
    ),
    ("राम का किताब मेज पर है", "राम की किताब मेज पर है।"),
    ("ravi ki ghar kal jaana hai", "Ravi ke ghar kal jaana hai."),
    ("kevin seen error in log", "Kevin saw the error in the log."),
    ("Neha aur Aman present tha", "Neha aur Aman present the."),
    ("server restart hui", "Server restart hua."),
    ("main jaata hai", "Main jaata hoon."),
    ("hum kal jayega", "Hum kal jayenge."),
    ("ट्रेनिंग स्लाइड्स तैयार हो गया", "ट्रेनिंग स्लाइड्स तैयार हो गईं।"),
    ("पेमेंट्स क्लियर हो गया था", "पेमेंट्स क्लियर हो गई थीं।"),
    ("दो वर्शन आज डेमो होगा", "दो वर्शन आज डेमो होंगे।"),
    ("वो लोग आ रहा है", "वो लोग आ रहे हैं।"),
    ("सौरभ ने अमाउंट चेक करती है और अप्रूवल भेजती है", "सौरभ ने अमाउंट चेक किया और अप्रूवल भेजा।"),
    ("Ship it today team. Also loop in Priya on the budget doc. Scratch that.", "Ship it today team."),
    ("i drafted the update for finance actually for operations", "I drafted the update for operations."),
    ("मेल राहुल को भेजो सॉरी रोहित को", "मेल रोहित को भेजो।"),
    ("मेल राहुल को भेजो, आई मीन रोहित को", "मेल रोहित को भेजो।"),
    ("रिपोर्ट सोमवार को भेजो एक्चुअली मंगलवार को", "रिपोर्ट मंगलवार को भेजो।"),
    ("ill send it", "I'll send it."),
    ("i will sent it", "I will send it."),
    ("main kal report bhejega", "Main kal report bhejunga."),
    ("main kal kaam karega", "Main kal kaam karunga."),
    ("main kal aayega", "Main kal aaunga."),
    ("hum kal payment dega", "Hum kal payment denge."),
    ("main file lega", "Main file lunga."),
    ("मैं कल रिपोर्ट भेजेगा", "मैं कल रिपोर्ट भेजूंगा।"),
    ("मैं कल काम करेगा", "मैं कल काम करूंगा।"),
    ("मैं कल आएगा", "मैं कल आऊंगा।"),
    ("हम कल पेमेंट देगा", "हम कल पेमेंट देंगे।"),
    ("मैं फाइल लेगा", "मैं फाइल लूंगा।"),
    ("she eated lunch", "She ate lunch."),
    ("he drinked the water", "He drank the water."),
    ("i sleeped late", "I slept late."),
    ("उसने चाय बनाया", "उसने चाय बनाई।"),
    ("उसने रोटी बनाया", "उसने रोटी बनाई।"),
    ("मैंने उसे बताई", "मैंने उसे बताया।"),
    ("first call sam and second email ana", "1. Call Sam\n2. Email Ana"),
    ("first call sam second email ana", "1. Call Sam\n2. Email Ana"),
    ("first the api second the api docs", "1. The API\n2. The API docs"),
    ("first add the airfare second add the hotel", "1. Add the airfare\n2. Add the hotel"),
    ("first set the cap to five second expand to ten percent", "1. Set the cap to 5\n2. Expand to 10%"),
    ("pehle report bhejo doosre report check karo", "1. Report bhejo\n2. Report check karo"),
    ("the first build failed the second build passed", "The first build failed, the second build passed."),
    ("pehla call sam ko doosra call ana ko", "Pehla call Sam ko, doosra call Ana ko."),
    (
        "the plan has two steps one call the vendor two update the sheet",
        "The plan has two steps:\n1. Call the vendor\n2. Update the sheet",
    ),
    ("हमारा रनवे बारह महीने का है", "हमारा रनवे 12 महीने का है।"),
    ("बीटा यूजर्स पंद्रह सौ हैं और रिटेंशन सत्तर प्रतिशत है", "बीटा यूजर्स 1,500 हैं और रिटेंशन 70% है।"),
    ("खर्चा पच्चीस हजार रुपये है और टैक्स अलग", "खर्चा ₹25,000 है और टैक्स अलग।"),
    ("बर्न रेट तीस हजार डॉलर है", "बर्न रेट $30,000 है।"),
    ("फिर बारह पंद्रह पर रिकवरी आई", "फिर 12:15 पर रिकवरी आई।"),
    ("कल फाइनेन्स टीम से बात हुई", "कल फाइनेंस टीम से बात हुई।"),
    ("यहाँ काम ज़्यादा है", "यहां काम ज्यादा है।"),
    ("सब हँसने लगे", "सब हंसने लगे।"),
    ("हेल्प डॉट इन खोलो", "help.in खोलो।"),
    ("मैं कल रिपोर्ट भेजता है", "मैं कल रिपोर्ट भेजता हूं।"),
    ("पेमेंट्स क्लियर हो गया था", "पेमेंट्स क्लियर हो गईं थीं।"),
    ("पोर्टल खोलिए डब्ल्यू डब्ल्यू डब्ल्यू डॉट हेल्प डॉट इन स्लैश रिफंड", "पोर्टल खोलिए www.help.in/refund।"),
    (
        "लिंक एच टी टी पी एस कोलन स्लैश स्लैश सर्विस डॉट कंपनी डॉट इन स्लैश एच ई एल पी है",
        "लिंक https://service.company.in/help है।",
    ),
    ("फाइल drive डॉट google डॉट com स्लैश folders स्लैश ops पर है", "फाइल drive.google.com/folders/ops पर है।"),
    ("उसे दो और फाइल चाहिए थी", "उसे दो और फाइलें चाहिए थीं।"),
    ("हमने टैक्स फाइल करता था", "हमने टैक्स फाइल किया था।"),
    ("प्लानिंग अगले हफ्ते शुरू होंगे", "प्लानिंग अगले हफ्ते शुरू होगी।"),
    ("मैंने रिटर्न भरती थी", "मैंने रिटर्न भरा था।"),
    ("स्लाइड्स तैयार हो गया और उसने देख लिया", "स्लाइड्स तैयार हो गईं और उसने देख लीं।"),
    ("server restart hui to service wapas aa gaya", "Server restart hua to service wapas aa gayi."),
    ("main status update karta hai", "Main status update karta hoon."),
    ("quality team ne material inspect karti hai", "Quality team ne material inspect kiya."),
    ("पेमेंट रन रविवार को नहीं बल्कि शनिवार रात आठ बजे होगा", "पेमेंट रन शनिवार रात 8 बजे होगा।"),
    (
        "send the reminder to emily at northstar dot io, no, emily at northstar dot com, actually sales at northstar "
        "dot com",
        "Send the reminder to sales@northstar.com.",
    ),
    ("nahi nahi tum next friday nahi sunday aao", "Tum next Sunday aao."),
    ("नहीं नहीं तुम अगले फ्राइडे नहीं संडे आओ", "तुम अगले संडे आओ।"),
    ("tum friday nahi sunday aao", "Tum Sunday aao."),
    ("main kal nahi parso aaunga", "Main parso aaunga."),
    ("मैं कल नहीं परसों आऊंगा", "मैं परसों आऊंगा।"),
    ("meet at five no six", "Meet at 6."),
    ("ship the hotfix to staging no to production", "Ship the hotfix to production."),
    ("put the notes in drive no in notion", "Put the notes in Notion."),
    ("i have 2 no 3 kids", "I have 3 kids."),
    ("mujhe 2 nahi 3 tickets chahiye", "Mujhe 3 tickets chahiye."),
    ("मुझे दो नहीं तीन टिकट चाहिए", "मुझे तीन टिकट चाहिए।"),
    ("book 3 tickets no 4", "Book 4 tickets."),
    ("mujhe 3 tickets nahi 4 chahiye", "Mujhe 4 tickets chahiye."),
    ("मुझे तीन टिकट नहीं चार चाहिए", "मुझे चार टिकट चाहिए।"),
    ("keep it with me no in your bag", "Keep it in your bag."),
    ("leave the keys with ravi no at the desk", "Leave the keys at the desk."),
    ("no no we meet friday", "We meet Friday."),
    ("pod delete karo matlab rehne do", "Rehne do."),
    ("पॉड डिलीट करो मतलब रहने दो", "रहने दो।"),
    ("file delete mat karo matlab rehne do", "Rehne do."),
    ("send the report to ravi no don't do this", "Don't do this."),
    ("send the report to ravi no don't send it yet", "Don't send it yet."),
    ("report ravi ko bhejo nahi mat bhejo", "Mat bhejo."),
    ("रिपोर्ट रवि को भेजो नहीं मत भेजो", "मत भेजो।"),
    ("ship it today, no, tomorrow", "Ship it tomorrow."),
    ("don't send it, no wait, send it", "Send it."),
    ("do not merge the pr scratch that merge the pr", "Merge the PR."),
    ("don't cancel the meeting i mean cancel the meeting", "Cancel the meeting."),
    ("mat bhejo, nahi nahi, bhejo", "Bhejo."),
    ("मत भेजो नहीं नहीं भेजो", "भेजो।"),
    ("the copy should say no busywork rather no distractions", "The copy should say no distractions."),
    ("kal sirf report bhejo nahi nahi parso", "Parso sirf report bhejo."),
    ("आज नहीं नहीं कल भेजो", "कल भेजो।"),
    ("मैंने उसे बताई", "मैंने उसे बताया।"),
    ("main kal report bhejega", "Main kal report bhejunga."),
    ("we ship it yesterday", "We shipped it yesterday."),
    ("pichle hafte hum review karte hain", "Pichle hafte hum review kiya."),
    ("call mom", "Call Mom."),
    ("your welcome", "You're welcome."),
    ("there car is here", "Their car is here."),
    ("this is better then that", "This is better than that."),
    ("who's file is this", "Whose file is this?"),
    ("its a good plan", "It's a good plan."),
    ("वह कल रिपोर्ट भेजता है", "वह कल रिपोर्ट भेजेगा।"),
    ("मैं कल आऊंगा", "मैं कल आऊंगा।"),
    ("उसने पैसे मांगा", "उसने पैसे मांगे।"),
    ("it almost work", "It almost works."),
    ("i have send it", "I have sent it."),
    ("he has went home", "He has gone home."),
    ("we have shipped it", "We shipped it."),
    ("she have paid the invoice", "She has paid the invoice."),
]


@pytest.mark.parametrize(("said", "wrote"), ACCEPTED)
def test_formatting_grammar_and_corrections_pass(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


REJECTED = [
    # The word must be the digit's own number, and a digit never becomes an ordinal.
    ("we have 2 options", "We have three options.", Reject.UNHEARD),
    ("2 kaam karo", "Teen kaam karo.", Reject.UNHEARD),
    ("2 काम करो", "तीन काम करो।", Reject.UNHEARD),
    ("call 1 person", "Call the first person.", Reject.UNHEARD),
    # Only a word right after itself is a stutter: a repeated clause stays, and so does a word said once.
    ("it's finished, it's finished", "It's finished.", Reject.TOO_MANY_CHANGES),
    ("we shipped shipped the build today", "We shipped the build.", Reject.QUALIFIER),
    ("I think this approach is probably better.", "I think this approach is superior.", Reject.UNHEARD),
    ("send the report today", "Send the 3 reports today.", Reject.UNHEARD),
    ("ship the fix today", "**Ship** the fix today.", Reject.UNHEARD),
    (
        "we should move the launch because the vendor contract is not signed yet",
        "We should move the launch.",
        Reject.TOO_MANY_CHANGES,
    ),
    ("deploy on tuesday then wednesday", "Deploy on Wednesday.", Reject.TOO_MANY_CHANGES),
    ("make the deck and change the font today", "Change the font today.", Reject.TOO_MANY_CHANGES),
    (
        "The release notes are ready and the changelog is merged into main. The dashboard tiles load "
        "slowly. The build is green and the staging deploy finished this morning.",
        "The release notes are ready and the changelog is merged into main. The build is green and the "
        "staging deploy finished this morning.",
        Reject.DROPPED_SENTENCE,
    ),
    (
        "can you write an email to the vendor about the delay",
        "Sure, can you write an email to the vendor about the delay?",
        Reject.REPLY,
    ),
    ("kal client ko call karna hai", "Call the client tomorrow.", Reject.UNHEARD),
    ("kya tum report bhej sakte ho", "Zaroor, kya tum report bhej sakte ho?", Reject.REPLY),
    ("कल रिपोर्ट भेजनी है", "kal report bhejni hai.", Reject.UNHEARD),
    ("कल रिपोर्ट भेजनी है", "Send the report tomorrow.", Reject.UNHEARD),
    ("क्या आप रिपोर्ट भेज सकते हैं", "ज़रूर, क्या आप रिपोर्ट भेज सकते हैं?", Reject.REPLY),
    ("send it to jamko", "Send it to GMMCO.", Reject.UNHEARD),
    ("send the invoice", "Sell the invoice.", Reject.UNHEARD),
    ("No problem, I will take it.", "I will take it.", Reject.TOO_MANY_CHANGES),
    ("there is no data in the report ship it today", "Ship it today.", Reject.TOO_MANY_CHANGES),
    ("mujhe nahi pata kya hua client ko call karo", "Client ko call karo.", Reject.TOO_MANY_CHANGES),
    ("मुझे नहीं पता क्या हुआ क्लाइंट को कॉल करो", "क्लाइंट को कॉल करो।", Reject.TOO_MANY_CHANGES),
    ("the budget actually doubled last quarter so we hire now", "So we hire now.", Reject.TOO_MANY_CHANGES),
    ("sorry for the delay the build broke last night we fixed it", "We fixed it.", Reject.TOO_MANY_CHANGES),
    ("we would rather ship friday than monday tell the team", "Tell the team.", Reject.TOO_MANY_CHANGES),
    (
        "send the deck to priya and wait for her reply before the launch",
        "Send the deck to Priya before the launch.",
        Reject.TOO_MANY_CHANGES,
    ),
    (
        "We should send this to the frontend team. No, don't send it yet.",
        "We should send this to the frontend team.",
        Reject.TOO_MANY_CHANGES,
    ),
    ("client ko bhejo. nahi, abhi mat bhejo", "Client ko bhejo.", Reject.TOO_MANY_CHANGES),
    ("क्लाइंट को भेजो। नहीं, अभी मत भेजो", "क्लाइंट को भेजो।", Reject.TOO_MANY_CHANGES),
    ("i am sorry the build broke again today", "The build broke again today.", Reject.TOO_MANY_CHANGES),
    ("i was wait listed for the conference", "Listed for the conference.", Reject.TOO_MANY_CHANGES),
    ("hai matlab report kal bhejni hai", "Report kal bhejni hai.", Reject.TOO_MANY_CHANGES),
    ("है मतलब रिपोर्ट कल भेजनी है", "रिपोर्ट कल भेजनी है।", Reject.TOO_MANY_CHANGES),
    ("we have no money left for the trip", "We have left for the trip.", Reject.NEGATION),
    ("i know the answer", "I don't know the answer.", Reject.NEGATION),
    ("i can go tomorrow", "I cannot go tomorrow.", Reject.NEGATION),
    ("i do not agree with the plan", "I do agree with the plan.", Reject.NEGATION),
    ("we will never ship this", "We will ship this.", Reject.NEGATION),
    ("main kal nahi aaunga", "Main kal aaunga.", Reject.NEGATION),
    ("मैं कल नहीं आऊंगा", "मैं कल आऊंगा।", Reject.NEGATION),
    ("kal meeting hai", "Kal meeting is.", Reject.UNHEARD),
    ("kal meeting hai", "Kal meeting है।", Reject.UNHEARD),
    ("यह काम का है", "यह काम of है।", Reject.UNHEARD),
    ("the file is ready", "The file hai ready.", Reject.UNHEARD),
    ("the meeting is at five", "The meeting is at 6.", Reject.UNHEARD),
    ("pay him fifty dollars", "Pay him $500.", Reject.UNHEARD),
    ("tell the team to wait for the review", "The review.", Reject.TOO_MANY_CHANGES),
    ("ravi said sorry about the delay", "About the delay.", Reject.TOO_MANY_CHANGES),
    ("the client is sorry we missed the deadline", "We missed the deadline.", Reject.TOO_MANY_CHANGES),
    ("priya was actually promoted last week", "Promoted last week.", Reject.TOO_MANY_CHANGES),
    ("i would rather go home now and rest", "Go home now and rest.", Reject.TOO_MANY_CHANGES),
    ("make that call to the bank before noon", "Call to the bank before noon.", Reject.TOO_MANY_CHANGES),
    ("is shabd ka matlab kya hai", "Kya hai?", Reject.TOO_MANY_CHANGES),
    ("इस शब्द का मतलब क्या है", "क्या है?", Reject.TOO_MANY_CHANGES),
    ("ship it", "Ship it to the and it or this with that for it in the.", Reject.TOO_MANY_CHANGES),
    ("call priya", "Call Priya and it.", Reject.TOO_MANY_CHANGES),
    ("invite alice and bob", "Invite Alice or Bob.", Reject.TOO_MANY_CHANGES),
    ("put it in the box", "Put it on the box.", Reject.TOO_MANY_CHANGES),
    ("meet at five not six", "Meet at 6, not 5.", Reject.UNHEARD),
    ("five people and six chairs", "6 people and 5 chairs.", Reject.UNHEARD),
    ("twenty five", "25 20", Reject.UNHEARD),
    ("the cost is five and six", "The cost is 56.", Reject.UNHEARD),
    ("accept the contract today", "Access the contract today.", Reject.UNHEARD),
    ("sign the contract now", "Sign the contrast now.", Reject.UNHEARD),
    ("send the mail to priya", "Send the main to Priya.", Reject.UNHEARD),
    ("the debit was approved", "The debt was approved.", Reject.UNHEARD),
    ("मैं बल्कि घर जाऊंगा आज", "घर जाऊंगा आज।", Reject.TOO_MANY_CHANGES),
    ("send a file to sam", "Send that file to Sam.", Reject.TOO_MANY_CHANGES),
    (
        "send the deck to sam and the notes to ravi scratch that the notes to priya",
        "The notes to Priya.",
        Reject.TOO_MANY_CHANGES,
    ),
    ("we need five servers by friday", "We need servers by Friday.", Reject.TOO_MANY_CHANGES),
    ("call me at three", "Call me.", Reject.TOO_MANY_CHANGES),
    ("ship the second build to sam today", "Ship the build to Sam today.", Reject.TOO_MANY_CHANGES),
    ("pehla draft accha tha", "Draft accha tha.", Reject.TOO_MANY_CHANGES),
    ("तीन लोग आएंगे", "लोग आएंगे।", Reject.TOO_MANY_CHANGES),
    ("पांच लोग आएंगे", "6 लोग आएंगे।", Reject.UNHEARD),
    ("ship it", "The ship is to the it a.", Reject.TOO_MANY_CHANGES),
    ("ship it now", "Ship the it to a now is.", Reject.TOO_MANY_CHANGES),
    ("kal meeting hai", "Kal ka meeting ki hai ke tha.", Reject.TOO_MANY_CHANGES),
    ("कल मीटिंग है", "कल का मीटिंग की है के था।", Reject.TOO_MANY_CHANGES),
    ("we are ready they are done it is live", "We were ready, they were done, it was live.", Reject.TOO_MANY_CHANGES),
    ("प्रोकोल डॉट इन पर रिपोर्ट भेजनी है", "procol.in par report bhejni hai.", Reject.UNHEARD),
    ("मेल प्रिया एट procol डॉट इन", "Mail priya@procol.in.", Reject.UNHEARD),
    ("बिल्स एट प्रोकोल डॉट इन पर भेजें", "sales@procol.in पर भेजें।", Reject.UNHEARD),
    ("के लिए हेल्प डॉट इन खोलें", "k liye help.in खोलें।", Reject.UNHEARD),
    ("Ravi will send the deck. Wait for it.", "For it.", Reject.TOO_MANY_CHANGES),
    ("Thanks for the update. Sorry for the delay on my side.", "For the delay on my side.", Reject.TOO_MANY_CHANGES),
    ("Send it to Sam. Sorry about that.", "About that.", Reject.TOO_MANY_CHANGES),
    ("We fly Monday. Wait until the visa comes.", "Until the visa comes.", Reject.TOO_MANY_CHANGES),
    ("Ship it today team. Also loop in Priya on the budget doc. Scratch that.", "", Reject.TOO_MANY_CHANGES),
    (
        "Ship it today team. Also loop in Priya on the budget doc. Scratch that.",
        "Also loop in Priya on the budget doc.",
        Reject.TOO_MANY_CHANGES,
    ),
    ("ship it today team also loop in priya on the budget doc scratch that", "", Reject.TOO_MANY_CHANGES),
    ("राहुल ने सॉरी बोला मीटिंग के लिए", "मीटिंग के लिए।", Reject.TOO_MANY_CHANGES),
    ("i was first in line and second in the race", "I was:\n1. In line\n2. In the race", Reject.TOO_MANY_CHANGES),
    ("first of the month and second of the month", "1. Of the month\n2. Of the month", Reject.TOO_MANY_CHANGES),
    ("we shipped one or two fixes", "We shipped:\n1. Or\n2. Fixes", Reject.TOO_MANY_CHANGES),
    ("humne ek ya do fix bheje", "Humne:\n1. Ya\n2. Fix bheje", Reject.TOO_MANY_CHANGES),
    ("हमने एक या दो फिक्स भेजे", "हमने:\n1. या\n2. फिक्स भेजे", Reject.TOO_MANY_CHANGES),
    ("we need one to two days", "We need:\n1. To\n2. Days", Reject.TOO_MANY_CHANGES),
    ("humein ek se do din chahiye", "Humein:\n1. Se\n2. Din chahiye", Reject.TOO_MANY_CHANGES),
    ("हमें एक से दो दिन चाहिए", "हमें:\n1. से\n2. दिन चाहिए", Reject.TOO_MANY_CHANGES),
    ("the first build failed the second build passed", "1. Build failed\n2. Build passed", Reject.TOO_MANY_CHANGES),
    (
        "mera pehla draft weak tha mera doosra draft strong tha",
        "1. Draft weak tha\n2. Draft strong tha",
        Reject.TOO_MANY_CHANGES,
    ),
    ("मेरा पहला ड्राफ्ट कमज़ोर था मेरा दूसरा ड्राफ्ट अच्छा था", "1. ड्राफ्ट कमज़ोर था\n2. ड्राफ्ट अच्छा था", Reject.TOO_MANY_CHANGES),
    ("main pehla aaya aur woh doosra", "Main:\n1. Aaya aur woh\n2.", Reject.TOO_MANY_CHANGES),
    ("i bought two apples one red and two green", "I bought two apples:\n1. Red\n2. Green", Reject.TOO_MANY_CHANGES),
    (
        "mere paas do kutte hain ek kala aur do safed",
        "Mere paas do kutte hain:\n1. Kala\n2. Safed",
        Reject.TOO_MANY_CHANGES,
    ),
    ("मेरे पास दो कुत्ते हैं एक काला और दो सफेद", "मेरे पास दो कुत्ते हैं:\n1. काला\n2. सफेद", Reject.TOO_MANY_CHANGES),
    ("i will send it", "I sent it.", Reject.TENSE),
    ("i will go there", "I go there.", Reject.TENSE),
    (
        "i will send the report and i will pay the vendor and i will call the client",
        "I sent the report and I paid the vendor and I called the client.",
        Reject.TENSE,
    ),
    ("main kal report bhejega", "Main kal report bheja.", Reject.UNHEARD),
    ("उसने चाय बनाई", "उसने चाय पाई।", Reject.UNHEARD),
    ("हमारा रनवे बारह महीने का है", "हमारा रनवे 13 महीने का है।", Reject.UNHEARD),
    ("रिव्यू साढ़े दस बजे होगा", "रिव्यू 11:30 बजे होगा।", Reject.UNHEARD),
    ("कुल बिल एक लाख बीस हजार आएगा", "कुल बिल ₹1,25,000 आएगा।", Reject.UNHEARD),
    ("बिल्स एट प्रोकोल डॉट इन पर भेजें", "bills@paypal.in पर भेजें।", Reject.UNHEARD),
    ("पोर्टल खोलिए डब्ल्यू डब्ल्यू डब्ल्यू डॉट हेल्प डॉट इन", "पोर्टल खोलिए www.help.in/admin।", Reject.UNHEARD),
    ("फाइल drive डॉट google डॉट com स्लैश ops पर है", "फाइल drive.google.com/slash ops पर है।", Reject.UNHEARD),
    ("रिपोर्ट कल भेजो", "Report kal bhejo.", Reject.UNHEARD),
    ("रिपोर्ट कल भेजो", "रिपोर्ट कल free भेजो।", Reject.UNHEARD),
    ("हमने टैक्स फाइल करता था", "हमने टैक्स फाइल रखा था।", Reject.UNHEARD),
    ("Neha aur Aman present tha", "Neha aur Aman absent the.", Reject.UNHEARD),
    ("पेमेंट रविवार को नहीं होगा", "पेमेंट रविवार को होगा।", Reject.NEGATION),
    ("हम बल्कि कल रिपोर्ट भेजेंगे और डेक भी", "कल रिपोर्ट भेजेंगे।", Reject.TOO_MANY_CHANGES),
    ("न सिर्फ़ रिपोर्ट बल्कि डेक भी भेजो", "डेक भी भेजो।", Reject.TOO_MANY_CHANGES),
    ("give one pill at night then give two pills at night", "Give two pills at night.", Reject.TOO_MANY_CHANGES),
    ("main friday nahi aaunga", "Main friday aaunga.", Reject.NEGATION),
    ("send it no wait send it", "Don't send it.", Reject.NEGATION),
    ("i have 2 no kids", "I have 2 kids.", Reject.NEGATION),
    ("मैं दो दिन नहीं आऊंगा", "मैं आऊंगा।", Reject.TOO_MANY_CHANGES),
    ("main 2 din nahi aaunga", "Main aaunga.", Reject.TOO_MANY_CHANGES),
    ("don't send it, no wait, send it", "Don't send it.", Reject.TOO_MANY_CHANGES),
    ("don't send it", "Send it.", Reject.NEGATION),
    (
        "the copy should say no busywork rather no distractions",
        "The copy should say distractions.",
        Reject.TOO_MANY_CHANGES,
    ),
    ("file delete mat karo", "File delete karo.", Reject.NEGATION),
    ("मैं फ्राइडे नहीं आऊंगा", "मैं फ्राइडे आऊंगा।", Reject.NEGATION),
    ("फाइल डिलीट मत करो", "फाइल डिलीट करो।", Reject.NEGATION),
    ("nahi nahi main friday nahi aaunga", "Main friday aaunga.", Reject.NEGATION),
    ("main friday nahi aaunga", "Main aaunga.", Reject.TOO_MANY_CHANGES),
    ("payment abhi nahi hoga", "Payment hoga.", Reject.TOO_MANY_CHANGES),
    ("abhi rollback nahi chahiye", "Abhi chahiye.", Reject.TOO_MANY_CHANGES),
    ("पेमेंट रविवार को नहीं होगा", "पेमेंट होगा।", Reject.TOO_MANY_CHANGES),
    ("मैं कल नहीं आऊंगा", "मैं आऊंगा।", Reject.TOO_MANY_CHANGES),
    ("tell the vendor no pickup after six pm", "Tell the pickup after 6 pm.", Reject.NEGATION),
    ("file delete mat karo matlab rehne do", "File delete karo.", Reject.TOO_MANY_CHANGES),
    ("pod delete karo matlab report bhejo", "Report bhejo.", Reject.TOO_MANY_CHANGES),
    ("send the report to ravi", "", Reject.DROPPED_SENTENCE),
    ("there is no data in the report don't ship it", "Don't ship it.", Reject.TOO_MANY_CHANGES),
    ("call priya today", "Call Priya.", Reject.QUALIFIER),
    ("maybe we ship friday", "We ship Friday.", Reject.QUALIFIER),
    ("charge the customer twice", "Charge the customer.", Reject.QUALIFIER),
    ("delete all files", "Delete files.", Reject.QUALIFIER),
    ("the bug is probably fixed", "The bug is fixed.", Reject.QUALIFIER),
    ("we must ship before friday", "We ship before Friday.", Reject.QUALIFIER),
    ("kal sirf report bhejo", "Kal report bhejo.", Reject.QUALIFIER),
    ("shayad kal meeting hai", "Kal meeting hai.", Reject.QUALIFIER),
    ("कल सिर्फ रिपोर्ट भेजो", "कल रिपोर्ट भेजो।", Reject.QUALIFIER),
    ("शायद कल मीटिंग है", "कल मीटिंग है।", Reject.QUALIFIER),
    ("main kal aaunga", "Main kal aaungi.", Reject.GENDER),
    ("main ghar ja raha hoon", "Main ghar ja rahi hoon.", Reject.GENDER),
    ("मैं कल आऊंगा", "मैं कल आऊंगी।", Reject.GENDER),
    ("मैं घर जा रहा हूं", "मैं घर जा रही हूं।", Reject.GENDER),
    ("हम घर जा रहे हैं", "हम घर जा रही हैं।", Reject.GENDER),
    ("we almost lost the client", "We lost the client.", Reject.QUALIFIER),
    ("we rarely ship on friday", "We ship on Friday.", Reject.QUALIFIER),
    ("it is definitely ready", "It is ready.", Reject.QUALIFIER),
    ("build lagbhag ready hai", "Build ready hai.", Reject.QUALIFIER),
    ("बिल्ड लगभग तैयार है", "बिल्ड तैयार है।", Reject.QUALIFIER),
    ("बिल्ड अक्सर फेल होता है", "बिल्ड फेल होता है।", Reject.QUALIFIER),
    ("we shipped it", "We ship it.", Reject.TENSE),
    ("i have sent the file to priya", "I send the file to Priya.", Reject.TENSE),
    ("we have finished the review", "We finish the review.", Reject.TENSE),
    ("she has paid the invoice", "She pays the invoice.", Reject.TENSE),
    ("i went home", "I go home.", Reject.TENSE),
    ("we ship the build", "We shipped the build.", Reject.TENSE),
    ("he is coming", "He was coming.", Reject.TENSE),
    ("i send it tomorrow", "I sent it tomorrow.", Reject.TENSE),
    ("मैं घर जाता हूं", "मैं घर जाता था।", Reject.TENSE),
    ("वह कल रिपोर्ट भेजेगा", "वह कल रिपोर्ट भेजा।", Reject.TENSE),
    ("मैं कल रिपोर्ट भेजूंगा", "मैं कल रिपोर्ट भेजता हूं।", Reject.TENSE),
    ("वो काम करेगा", "वो काम करता है।", Reject.TENSE),
    ("kal meeting hai", "Kal meeting thi.", Reject.TENSE),
    ("कल मीटिंग है", "कल मीटिंग थी।", Reject.TENSE),
    ("your file is ready", "Their file is ready.", Reject.UNHEARD),
    ("send report", "I send the report.", Reject.TOO_MANY_CHANGES),
    ("pay it", "Pay for it.", Reject.TOO_MANY_CHANGES),
]


@pytest.mark.parametrize(("said", "wrote", "reason"), REJECTED)
def test_unheard_words_rewrites_dropped_sentences_and_replies_fail(said: str, wrote: str, reason: Reject) -> None:
    assert check(said, wrote, []) == reason


def test_decomposed_and_precomposed_unicode_are_the_same_word() -> None:
    said = unicodedata.normalize("NFD", "ज़रूरी काम है")
    wrote = "\u095bरूरी काम है।"
    assert said[0] != wrote[0]
    assert check(said, wrote, []) is None


@pytest.mark.parametrize("span", [["aaj", "nahi", "nahi"], ["आज", "नहीं", "नहीं"], ["today", "no", "no"]])
def test_the_last_cue_is_the_longest_one_ending_last(span: list[str]) -> None:
    assert last_cue(span) == (1, 3)


def test_a_lone_cue_cannot_erase_the_prior() -> None:
    prior = "Hi team, the launch moved to Monday and legal signed off on the contract."
    judged = judge(prior, "actually", [], "", "")
    assert judged == Polished(text="actually", revision=None, rejected=Reject.TOO_MANY_CHANGES)


def test_an_everyday_wait_for_cannot_erase_the_prior() -> None:
    judged = judge("Ravi will send the deck.", "wait for it", [], "", "For it.")
    assert judged.rejected == Reject.TOO_MANY_CHANGES


def test_a_chunk_that_only_scratches_the_last_sentence_revises_the_prior() -> None:
    prior = "Ship it today team. Also loop in Priya on the budget doc."
    judged = judge(prior, "scratch that", [], "Ship it today team.", "")
    assert judged == Polished(text="", revision="Ship it today team.", rejected=None)


def test_a_retraction_of_the_whole_prior_is_kept() -> None:
    prior = "We should send this to the frontend team."
    judged = judge(prior, "no, don't send it yet", [], "", "Don't send it yet.")
    assert judged == Polished(text="Don't send it yet.", revision="", rejected=None)


@pytest.mark.parametrize("path", sorted(CORPUS.glob("*.jsonl.gz")), ids=lambda path: path.name)
def test_the_guardrail_accepts_the_corpus_gold_answers(path: Path) -> None:
    with gzip.open(path, "rt", encoding="utf-8") as lines:
        rows = [json.loads(line) for line in lines]
    rejected = [row["id"] for row in rows if gold_refused(row)]
    assert len(rejected) * 100 <= len(rows), rejected


LONE_NEGATORS = {"no", "nahi", "nahin", "नहीं"}


def without_a_negation(text: str) -> str | None:
    """The text with its first lone "no", "nahi" or "नहीं" and the word before it dropped, as a model that misread
    a negation as a correction would write it; `None` when it has none."""
    found = text.split(" ")
    for i in range(1, len(found) - 1):
        word = found[i].lower()
        doubled = found[i - 1].lower().strip(",") in LONE_NEGATORS or found[i + 1].lower() in LONE_NEGATORS
        if word in LONE_NEGATORS and not doubled and found[i - 1][-1].isalpha():
            return " ".join(found[: i - 1] + found[i + 1 :])
    return None


def test_dropping_a_real_negation_with_the_word_before_it_is_rejected_across_the_corpus() -> None:
    mutated = []
    for path in sorted(CORPUS.glob("*.jsonl.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as lines:
            for row in map(json.loads, lines):
                wrote = without_a_negation(row["expected"][0])
                if wrote is not None:
                    mutated.append((row["id"], check(row["said"], wrote, row["dictionary"])))
    accepted = [row_id for row_id, reason in mutated if reason is None]
    assert len(mutated) >= 80
    assert len(accepted) * 100 <= len(mutated), accepted


def test_a_term_may_appear_though_the_user_said_it_differently() -> None:
    assert check("send it to jamko", "Send it to GMMCO.", ["GMMCO"]) is None


def test_tidy_puts_term_spelling_back_and_trims_line_ends() -> None:
    tidied = tidy("Gmmco signed, and IOS ships Friday.  \n- Priya ", ["GMMCO", "iOS", "Priya"])
    assert tidied == "GMMCO signed, and iOS ships Friday.\n- Priya"


def test_tidy_keeps_a_sentence_start_capital_on_a_lowercase_term() -> None:
    text = "Main kal office aaunga. Bahut kaam Hai na? hai.\n1. Bahut"
    tidied = tidy(text, ["main", "kal", "aaunga", "bahut", "hai"])
    assert tidied == "Main kal office aaunga. Bahut kaam hai na? hai.\n1. Bahut"


def test_tidy_lowercases_a_lowercase_term_inside_a_sentence() -> None:
    assert tidy("Use NPM and Bahut more.", ["npm", "bahut"]) == "Use npm and bahut more."


def test_tidy_leaves_a_term_inside_another_word_alone() -> None:
    assert tidy("The iOSX build and ios ship.", ["iOS"]) == "The iOSX build and iOS ship."


PRIOR = "Let's meet on Friday at noon."
FIX = "no wait make it thursday not friday"


def test_a_revision_is_judged_with_the_prior_text() -> None:
    judged = judge(PRIOR, FIX, [], "Let's meet on Thursday at noon.", "")
    assert judged == Polished(text="", revision="Let's meet on Thursday at noon.", rejected=None)


def test_a_revision_with_an_unheard_word_is_rejected_whole() -> None:
    judged = judge(PRIOR, FIX, [], "Let's meet on Thursday at lunch.", "")
    assert judged == Polished(text=FIX, revision=None, rejected=Reject.UNHEARD)


def test_a_rejected_answer_keeps_the_request_text() -> None:
    judged = judge("", "ship it", [], None, "Sure, shipping it.")
    assert judged == Polished(text="ship it", revision=None, rejected=Reject.REPLY)


def test_an_accepted_answer_is_tidied() -> None:
    judged = judge("", "ship it to gmmco", ["GMMCO"], None, "Ship it to Gmmco.  ")
    assert judged == Polished(text="Ship it to GMMCO.", revision=None, rejected=None)


def test_a_revision_without_prior_text_or_equal_to_it_is_dropped() -> None:
    assert judge("", "ship it", [], "Ship it.", "Ship it.").revision is None
    assert judge(PRIOR, "and bring snacks", [], PRIOR, "And bring snacks.").revision is None


def test_hinglish_revision_reaching_into_prior() -> None:
    judged = judge("Kal meeting rakhte hain.", "nahi nahi parso rakhte hain", [], "Parso meeting rakhte hain.", "")
    assert judged.rejected is None
    assert judged.revision == "Parso meeting rakhte hain."


def test_a_first_chunk_answered_only_as_a_revision_is_judged_as_its_text() -> None:
    judged = judge("", "she go home", [], "She goes home.", "")
    assert judged == Polished(text="She goes home.", revision=None, rejected=None)
    unheard = judge("", "she go home", [], "She goes home now.", "")
    assert unheard == Polished(text="she go home", revision=None, rejected=Reject.UNHEARD)


def test_a_negation_dropped_across_chunks_is_rejected() -> None:
    judged = judge("Main Friday nahi aaunga.", "aur report bhejo", [], "Main aaunga.", "Aur report bhejo.")
    assert judged.rejected == Reject.NEGATION


def test_a_negative_instruction_taken_back_across_chunks_is_kept() -> None:
    judged = judge("Don't send it.", "no wait send it", [], "Send it.", "")
    assert judged == Polished(text="", revision="Send it.", rejected=None)


def test_hindi_revision_reaching_into_prior() -> None:
    judged = judge("कल मीटिंग रखते हैं।", "नहीं नहीं परसों", [], "परसों मीटिंग रखते हैं।", "")
    assert judged.rejected is None
    assert judged.revision == "परसों मीटिंग रखते हैं।"


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("kal meeting hai", "Kal meeting thi."),
        ("the tests are passing", "The tests were passing."),
        ("कल मीटिंग है", "कल मीटिंग थी।"),
    ],
)
def test_a_tense_swap_costs_one_change(said: str, wrote: str) -> None:
    said_tokens = tokens(said)
    said_words = [word for word, _ in said_tokens]
    wrote_words = [word for word, _ in tokens(wrote)]
    marks = [mark for _, mark in said_tokens]
    count, _, _, _ = changed(said_words, marks, wrote_words, align(said_words, wrote_words), set())
    assert count == 1


@pytest.mark.parametrize(
    ("said", "wrote", "polished"),
    [
        ("i will try again on hotel wifi", "I will try again on hotel Wi-Fi.", "I will try again on hotel wifi."),
        ("oncall ko ping karo", "On-call ko ping karo.", "Oncall ko ping karo."),
        ("oncall टीम को बताओ", "On-call टीम को बताओ।", "Oncall टीम को बताओ।"),
    ],
)
def test_a_word_said_whole_loses_the_hyphen_the_model_put_in(said: str, wrote: str, polished: str) -> None:
    assert judge("", said, [], None, wrote) == Polished(text=polished, revision=None, rejected=None)


@pytest.mark.parametrize(
    ("said", "wrote", "terms"),
    [
        ("send a follow up email", "Send a follow-up email.", []),
        ("kal check in karna hai", "Kal check-in karna hai.", []),
        ("follow up कॉल करो", "Follow-up कॉल करो।", []),
        ("page the oncall", "Page the on-call.", ["on-call"]),
    ],
)
def test_a_hyphen_between_words_said_apart_or_in_a_term_stays(said: str, wrote: str, terms: list[str]) -> None:
    assert judge("", said, terms, None, wrote) == Polished(text=wrote, revision=None, rejected=None)


def test_a_revision_loses_the_hyphen_the_model_put_in_a_word_said_whole() -> None:
    judged = judge("Ping the team.", "no wait ping the oncall", [], "Ping the on-call.", "")
    assert judged == Polished(text="", revision="Ping the oncall.", rejected=None)


JOINS = [
    # A piece that goes on with the sentence before it keeps the word joining it (founder, 11 Oct: "visually
    # appealing How is it going to show me the data?").
    (
        "2. How well is it going to be, visually appealing",
        "and how is it going to show me the data?",
        "How is it going to show me the data?",
        Reject.DROPPED_JOIN,
    ),
    (
        "2. How well is it going to be, visually appealing",
        "and how is it going to show me the data?",
        "and how is it going to show me the data?",
        None,
    ),
    ("We shipped it.", "and then we celebrated", "Then we celebrated.", None),
    ("pehle report bhejo", "phir priya ko call karo", "Priya ko call karo.", Reject.DROPPED_JOIN),
    ("pehle report bhejo", "phir priya ko call karo", "phir Priya ko call karo.", None),
    ("रिपोर्ट भेजो", "फिर प्रिया को फोन करो", "प्रिया को फोन करो।", Reject.DROPPED_JOIN),
    ("रिपोर्ट भेजो", "और प्रिया को फोन करो", "और प्रिया को फोन करो।", None),
]


@pytest.mark.parametrize(("prior", "text", "output", "reason"), JOINS)
def test_a_piece_that_continues_a_sentence_keeps_its_joining_word(
    prior: str, text: str, output: str, reason: Reject | None
) -> None:
    assert judge(prior, text, [], None, output).rejected == reason


@pytest.mark.parametrize(
    ("prior", "text", "output", "expected"),
    [
        ("2. visually appealing", "and How is it going", "And how is it going?", "and how is it going?"),
        (
            "2. Kitna accha dikhta hai",
            "Aur Data kaise dikhata hai?",
            "Aur data kaise dikhata hai?",
            "aur data kaise dikhata hai?",
        ),
        ("We shipped it.", "and then we left", "And then we left.", "And then we left."),
    ],
)
def test_a_joining_word_that_goes_on_with_a_sentence_comes_back_lowercase(
    prior: str, text: str, output: str, expected: str
) -> None:
    assert judge(prior, text, [], None, output).text == expected


# A word the recognizer misheard may become the word that sounds the same and was meant (STT policy, 11 Oct): one
# said word for one written, two for one ("a head" ahead) or one for two ("bhejdiya" bhej diya), same script.
MISHEARD = [
    ("we need to taste the build before friday", "We need to test the build before Friday.", []),
    ("it was a waist of time", "It was a waste of time.", []),
    ("check weather it works", "Check whether it works.", []),
    ("one peace is missing from the box", "One piece is missing from the box.", []),
    ("open the write file first", "Open the right file first.", []),
    ("take a brake after lunch", "Take a break after lunch.", []),
    ("we are a head of the launch", "We are ahead of the launch.", []),
    ("i for got the keys at home", "I forgot the keys at home.", []),
    ("please except the offer by friday", "Please accept the offer by Friday.", []),
    ("you should of told me earlier", "You should have told me earlier.", []),
    ("i no the florist", "I know the florist.", []),
    ("the clerk new my number", "The clerk knew my number.", []),
    ("send male today", "Send mail today.", []),
    ("it past review", "It passed review.", []),
    ("witch date works", "Which date works?", []),
    ("the service can cash the results", "The service can cache the results.", []),
    ("main usko kal karunga", "Main usko call karunga.", []),
    ("sub log dashboard dekh rahe the", "Sab log dashboard dekh rahe the.", []),
    ("registry may fresh petition aayi", "Registry me fresh petition aayi.", []),
    ("claim approval aagaya", "Claim approval aa gaya.", []),
    ("ऑफिस मैं मीटिंग है", "ऑफिस में मीटिंग है।", []),
    ("उसने कहा की वो आएगा", "उसने कहा कि वो आएगा।", []),
    ("मुझे इसकी चिता है", "मुझे इसकी चिंता है।", []),
    ("we pay invoices through zero", "We pay invoices through Xero.", ["Xero"]),
    ("book a ride on lift tonight", "Book a ride on Lyft tonight.", ["Lyft"]),
    ("the lift is broken again", "The lift is broken again.", ["Lyft"]),
    ("ask procall for the quote", "Ask Procol for the quote.", ["Procol"]),
]


@pytest.mark.parametrize(("said", "wrote", "terms"), MISHEARD)
def test_a_misheard_word_may_become_the_sound_alike_that_was_meant(said: str, wrote: str, terms: list[str]) -> None:
    assert check(said, wrote, terms) is None


MISHEARD_REFUSED = [
    ("taste the build", "Check the build.", [], Reject.UNHEARD),
    ("we need fifteen chairs", "We need 50 chairs.", [], Reject.UNHEARD),
    ("i can come", "I can't come.", [], Reject.NEGATION),
    ("tell sean the plan", "Tell Shawn the plan.", [], Reject.UNHEARD),
    ("tell sean the plan", "Tell Shaun the plan.", [], Reject.UNHEARD),
    ("Lakshmi will join the call", "Laxmi will join the call.", ["Lakshmi"], Reject.UNHEARD),
    ("we use Xero for invoices", "We use zero for invoices.", ["Xero"], Reject.UNHEARD),
    ("main kal aaunga", "Main kal aaungi.", [], Reject.GENDER),
    ("i will send it", "I sent it.", [], Reject.TENSE),
    ("book a ride on lift tonight", "Book a ride on Lyft tonight.", [], Reject.UNHEARD),
    ("send it", "Send it to Procol.", ["Procol"], Reject.UNHEARD),
    ("taste the waist of the peace", "Test the waste of the piece.", [], Reject.TOO_MANY_CHANGES),
    ("deploy it on Kubernetes today", "Deploy it on Kubernete today.", ["Kubernetes"], Reject.TERM),
    ("we have no time left", "We have now time left.", [], Reject.UNHEARD),
]


@pytest.mark.parametrize(("said", "wrote", "terms", "reason"), MISHEARD_REFUSED)
def test_a_sound_alike_never_changes_a_fact_a_name_or_a_term(
    said: str, wrote: str, terms: list[str], reason: Reject
) -> None:
    assert check(said, wrote, terms) == reason


LISTS_AND_NUMBERS = [
    # "And three" counts the last item off, like "one" and "two" before it (founder-020, 11 Oct).
    (
        "Put these in a numbered list. One, milk. Two, eggs. And three, bread.",
        "Put these in a numbered list:\n1. Milk\n2. Eggs\n3. Bread",
    ),
    (
        "Ye numbered list mein chahiye. Ek, design final karo. Do, pricing page banao. Aur teen, sales ko brief karo.",
        "Ye numbered list mein chahiye:\n1. Design final karo\n2. Pricing page banao\n3. Sales ko brief karo",
    ),
    (
        "मुझे ये लिस्ट में चाहिए। एक, बैंक जाना है। दो, बिल भरना है। और तीन, माँ को फोन करना है।",
        "मुझे ये लिस्ट में चाहिए:\n1. बैंक जाना है\n2. बिल भरना है\n3. माँ को फोन करना है",
    ),
    # An item after the counted ones takes the next number, and a dangling last word goes (founder-021).
    (
        "i need to do the following first call the bank second pay the rent. Pick up the dry cleaning. And",
        "I need to do the following:\n1. Call the bank\n2. Pay the rent\n3. Pick up the dry cleaning",
    ),
    # Items asked for in a numbered list are numbered though not counted (D5).
    (
        "I want the following in a numbered list. Buy milk. Call mom. Pay the rent.",
        "I want the following in a numbered list:\n1. Buy milk\n2. Call mom\n3. Pay the rent",
    ),
    (
        "here are a few things about me. i lead the design team. i love long hikes. i live in pune.",
        "Here are a few things about me:\n- I lead the design team\n- I love long hikes\n- I live in Pune",
    ),
    ("सवा लीटर जूस", "1.25 लीटर जूस।"),
    ("डेढ़ बजे बीस डॉलर", "1:30 बजे $20।"),
    ("बजट ढाई लाख रुपये", "बजट ₹2.5 लाख।"),
    ("साढ़े तीन प्रतिशत ब्याज", "3.5% ब्याज।"),
    ("टीम पौने छह बजे आई", "टीम 5:45 बजे आई।"),
    ("मकान मालिक को साढ़े पंद्रह हजार रुपये देने हैं", "मकान मालिक को ₹15,500 देने हैं।"),
    ("सवा लाख का पैकेज", "₹1.25 लाख का पैकेज।"),
    ("ढाई हज़ार रुपये भेजो", "₹2,500 भेजो।"),
    ("sava paanch baje aana", "5:15 baje aana."),
    ("dedh kilo aata lana", "1.5 kilo aata lana."),
    ("dhai baje call hai", "2:30 baje call hai."),
    ("paune do lakh rupaye", "₹1.75 lakh."),
    ("the truck reaches quarter to six", "The truck reaches 5:45."),
    ("thirteen interviews at 3pm", "13 interviews at 3 pm."),
    ("नंबर नौ आठ एक शून्य दो है", "नंबर 98102 है।"),
    ("बारहवीं की मार्कशीट लाओ", "12वीं की मार्कशीट लाओ।"),
    ("neha ne file check kar liya", "Neha ne file check kar li."),
    ("maine packing list bana diya", "Maine packing list bana di."),
    ("राशि खाते पर जमा हो गई", "राशि खाते में जमा हो गई।"),
    ("ye may be kal hoga", "Ye maybe kal hoga."),
]


@pytest.mark.parametrize(("said", "wrote"), LISTS_AND_NUMBERS)
def test_counted_lists_fractions_and_agreement_fixes_pass(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("सवा पांच बजे", "6:15 बजे।"),
        ("डेढ़ बजे आना", "2:30 बजे आना।"),
        ("the truck reaches quarter to six", "The truck reaches 6:45."),
        ("पौने छह बजे", "6:45 बजे।"),
        (
            "I want the following in a numbered list. Buy milk. Call mom.",
            "I want the following:\n1. Buy milk\n5. Call mom",
        ),
        ("one red and two green", "1. Red\n2. Green"),
    ],
)
def test_a_wrong_fraction_or_list_number_is_refused(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is not None


@pytest.mark.parametrize(
    ("said", "wrote", "terms", "expected"),
    [
        ("ship it today", "Okay, ship it today.", [], Reason(Reject.UNHEARD, ("okay",))),
        ("we shipped it", "We ship it.", [], Reason(Reject.TENSE, ("shipped",))),
        ("i can come", "I can't come.", [], Reason(Reject.NEGATION, ("can't",))),
        ("deploy it on Kubernetes", "Deploy it on Kubernete.", ["Kubernetes"], Reason(Reject.TERM, ("Kubernetes",))),
        ("sure i can take it", "Sure, I can take it.", [], None),
    ],
)
def test_a_refusal_names_its_rule_and_the_words(said: str, wrote: str, terms: list[str], expected: Reason) -> None:
    assert verdict(said, wrote, terms) == expected


def test_a_rejected_polish_carries_its_reason() -> None:
    judged = judge("", "ship it today", [], None, "Okay, ship it today.")
    assert judged.reason == Reason(Reject.UNHEARD, ("okay",))
    assert judged == Polished(text="ship it today", revision=None, rejected=Reject.UNHEARD)


def test_a_dropped_joining_word_is_named() -> None:
    dropped = judge("2. visually appealing", "and how is it going", [], None, "How is it going?")
    assert dropped.reason == Reason(Reject.DROPPED_JOIN, ("and",))


# Round 1 (11 Oct): right answers the guardrail refused, in English, Hinglish and Hindi.
ACCEPTED_ROUND_1 = [
    # A lone "no" between two names, colours, sides, levels or family words takes the first back.
    (
        "the family wants to see amy no emily for the pain consult",
        "The family wants to see Emily for the pain consult.",
    ),
    ("send it to priya no rahul", "Send it to Rahul."),
    ("call mom no dad", "Call Dad."),
    ("use red no blue for the header", "Use blue for the header."),
    ("set it to high no low priority", "Set it to low priority."),
    (
        "the text block should align left no center and the note under it should say draft only actually internal "
        "draft",
        "The text block should align center and the note under it should say internal draft.",
    ),
    # "no wait" and "no actually" are strong cues: a new instruction after them replaces the one before.
    ("move it to the backlog no actually close it", "Close it."),
    ("approve it no wait reject it", "Reject it."),
    # Marks said as words, in pairs and in Hindi.
    ("she said open quote hi close quote", 'She said "hi."'),
    ("पहली बात नया पैराग्राफ दूसरी बात", "पहली बात।\n\nदूसरी बात।"),
    ("नमस्ते नई लाइन कैसे हो", "नमस्ते\nकैसे हो?"),
    ("क्या तुम आओगे क्वेश्चन मार्क", "क्या तुम आओगे?"),
    # English number words said in Devanagari.
    ("क्या टेबल वर्जन टू भेजा था या वर्जन थ्री", "क्या टेबल वर्जन 2 भेजा था या वर्जन 3?"),
    # A real negation stays, and so does a lone "no" before a negation phrase.
    ("the patient has no allergies actually no known allergies", "The patient has no known allergies."),
    ("june fifth no one objected", "June 5. No one objected."),
    # "not friday" names the version taken back.
    ("let's meet on friday at noon no wait make it thursday not friday", "Let's meet on Thursday at noon."),
    # "X nahi Y" after a cue is the replacement, not a negation of it.
    (
        "प्रिय नेहा, mail में rate card जोड़ देना, sorry latest rate card नहीं पुराना वाला.",
        "प्रिय नेहा, mail में पुराना वाला rate card जोड़ देना।",
    ),
]


@pytest.mark.parametrize(("said", "wrote"), ACCEPTED_ROUND_1)
def test_round_1_right_answers_pass(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


# Round 1 (11 Oct): wrong answers the guardrail let through.
REJECTED_ROUND_1 = [
    # A negation after the cue belongs to the replacement; a weak cue before "for" or "about" is a verb.
    ("send it no wait don't send it", "Send it.", Reject.TOO_MANY_CHANGES),
    (
        "the patient has no allergies actually no known allergies",
        "The patient has known allergies.",
        Reject.TOO_MANY_CHANGES,
    ),
    ("i will wait for the report", "I will the report.", Reject.TOO_MANY_CHANGES),
    ("sorry about the delay", "About the delay.", Reject.TOO_MANY_CHANGES),
    ("june fifth no one objected", "June 5. One objected.", Reject.NEGATION),
    (
        "we need the sales snapshot for 2022 no for 2021 to compare",
        "We need the sales snapshot for 2022 to compare.",
        Reject.TOO_MANY_CHANGES,
    ),
    # Both versions kept around a cue.
    ("ship friday i mean monday", "Ship Friday, I mean Monday.", Reject.KEPT_CORRECTION),
    ("the meeting is monday no tuesday", "The meeting is Monday, no, Tuesday.", Reject.KEPT_CORRECTION),
    ("see amy no emily", "See Amy no Emily.", Reject.KEPT_CORRECTION),
    # A question made a statement, a mark word used as a noun eaten as a mark.
    ("is the deck ready", "The deck is ready.", Reject.QUESTION),
    ("the period of the contract is two years", "The. Of the contract is 2 years.", Reject.TOO_MANY_CHANGES),
    # Lists: one never announced, one missing an item.
    ("I woke up then made coffee then left", "- I woke up\n- Made coffee\n- Left", Reject.FALSE_LIST),
    ("bullet milk bullet eggs bullet bread", "- Milk\n- Eggs", Reject.DROPPED_ITEM),
    (
        "first verify the payment schedule second review late fee language third confirm invoice timing",
        "1. Verify the payment schedule\n3. Confirm invoice timing",
        Reject.DROPPED_ITEM,
    ),
    (
        "first verify the payment schedule second review late fee language third confirm invoice timing",
        "1. Verify the payment schedule\n2. Confirm invoice timing",
        Reject.DROPPED_SENTENCE,
    ),
    # A sentence left out whose words recur elsewhere, with a cue far from it.
    (
        "the vendor sent the contract to our legal team this morning and the finance team reviewed the pricing in "
        "the contract after lunch. the legal team wants a clean copy of the contract from the vendor. the vendor "
        "will ship the first order to the warehouse on monday and the finance team will pay the invoice next week "
        "actually the week after",
        "The vendor sent the contract to our legal team this morning, and the finance team reviewed the pricing in "
        "the contract after lunch. The vendor will ship the first order to the warehouse on Monday, and the finance "
        "team will pay the invoice the week after.",
        Reject.DROPPED_SENTENCE,
    ),
    # A word translated.
    (
        "notes mein likha hai ki table rows ko alternate shade do",
        "Notes in likha hai ki table rows ko alternate shade do.",
        Reject.TRANSLATED,
    ),
    (
        "the api is not broken bas auth header nahi ja raha hai",
        "The API is not broken bas auth header not ja raha hai.",
        Reject.TRANSLATED,
    ),
    ("वर्कर pool अभी खाली है", "वर्कर pool अभी खाली is।", Reject.TRANSLATED),
    # A meridiem never said, and a qualifier dropped beside a plain "मतलब".
    ("the meeting is from two to three pm", "The meeting is from 2 to 3 am.", Reject.QUALIFIER),
    ("यह फीडबैक कॉल का मतलब सिर्फ क्लैरिटी लेना है", "यह फीडबैक कॉल का मतलब क्लैरिटी लेना है।", Reject.QUALIFIER),
]


@pytest.mark.parametrize(("said", "wrote", "reason"), REJECTED_ROUND_1)
def test_round_1_wrong_answers_fail(said: str, wrote: str, reason: Reject) -> None:
    assert check(said, wrote, []) == reason


def test_a_misspelled_term_is_refused_though_a_cue_follows_it() -> None:
    said = "ship the android build on thursday actually on friday morning"
    assert check(said, "Ship the Androi build on Friday morning.", ["Android"]) == Reject.TERM
    assert check(said, "Ship the Android build on Friday morning.", ["Android"]) is None


def test_a_term_taken_back_whole_by_a_cue_is_not_lost() -> None:
    assert check("i can have ben handle this rather nina", "I can have Nina handle this.", ["Ben"]) is None


def test_a_meridiem_said_apart_or_joined_is_heard() -> None:
    assert check("call me at nine a m before the showing", "Call me at 9 am before the showing.", []) is None
    assert check("thirteen interviews at 3pm", "13 interviews at 3 pm.", []) is None


# Round 3 (11 Oct): right misheard-word fixes the guardrail refused. A number word may be fixed for its sound-alike
# ("to tickets" two) though never into another number; a cue word may be written for its sound-alike ("weight"
# wait); silent letters don't count ("reign" rain); a split word may join though a part is a number ("ten your"
# tenure); and pairs speech to text confuses a syllable apart are known ("bones" bonus).
MISHEARD_ROUND_3 = [
    ("i bought to tickets for the show", "I bought two tickets for the show."),
    ("the queue is two long", "The queue is too long."),
    ("we have for options", "We have four options."),
    ("the meeting is at for pm", "The meeting is at 4 pm."),
    ("the team one the match", "The team won the match."),
    ("please weight in the lobby until your name is called", "Please wait in the lobby until your name is called."),
    ("the weather forecast says it will reign all weekend", "The weather forecast says it will rain all weekend."),
    ("the home loan has a ten your of twenty years", "The home loan has a tenure of 20 years."),
    ("the weather four cast says heavy rain on saturday", "The weather forecast says heavy rain on Saturday."),
    ("home loan ka ten your fifteen years set kiya hai", "Home loan ka tenure 15 years set kiya hai."),
    ("होम लोन का टेन योर पंद्रह साल रखा है", "होम लोन का टेन्योर 15 साल रखा है।"),
    ("the referral bones for analyst hires is two thousand dollars", "The referral bonus for analyst hires is $2,000."),
    ("is saal diwali bones kab aayega", "Is saal Diwali bonus kab aayega?"),
    ("my doctor put me on a five day antibiotic chorus", "My doctor put me on a 5-day antibiotic course."),
    (
        "my grandmother was formally a teacher at the village school",
        "My grandmother was formerly a teacher at the village school.",
    ),
    ("we should negate a better rate with the landlord", "We should negotiate a better rate with the landlord."),
    ("yeah se station sirf das minute hai auto le lo", "Yaha se station sirf 10 minute hai, auto le lo."),
    ("yeah aao", "Yaha aao."),
    ("कॉलेज ने एडमिशन के लिए माय ग्रेशन सर्टिफिकेट मांगा है", "कॉलेज ने एडमिशन के लिए माइग्रेशन सर्टिफिकेट मांगा है।"),
    ("सर्वर का लाउड टेस्ट कल रात चलाना है", "सर्वर का लोड टेस्ट कल रात चलाना है।"),
]


@pytest.mark.parametrize(("said", "wrote"), MISHEARD_ROUND_3)
def test_round_3_misheard_number_cue_and_near_sound_fixes_pass(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        # A number never becomes another number, and a right word stays.
        ("we need fifteen chairs", "We need 50 chairs."),
        ("i want to go home", "I want two go home."),
        ("the debit was approved", "The debt was approved."),
        ("we have two options", "We have 3 options."),
        ("we have two options", "We have too options."),
    ],
)
def test_round_3_a_number_sound_alike_never_changes_a_count(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is not None


# Round 3: right grammar fixes the guardrail refused. A past after a negated modal is a slip for the base form; a
# verb agrees with its own clause's subject, not with a "main" in another clause; a future time word allows "will"
# and a future form, a past one a past form; Hindi plurals are forms of their noun; Indian English "from morning"
# becomes "since morning".
GRAMMAR_ROUND_3 = [
    ("he did not went yesterday", "He did not go yesterday."),
    ("she did not gave the keys", "She did not give the keys."),
    ("they could not found the bug", "They could not find the bug."),
    ("we will not sent the file today", "We will not send the file today."),
    ("she can't came today", "She can't come today."),
    ("meri gaadi kharab ho gaya isliye main late hua", "Meri gaadi kharab ho gayi, isliye main late hua."),
    ("baarish ruk gaya toh hum nikal jayenge", "Baarish ruk gayi toh hum nikal jayenge."),
    ("बारिश रुक गया तो हम चलेंगे", "बारिश रुक गई तो हम चलेंगे।"),
    ("मेरी किताब खो गया तो मैं परेशान हूं", "मेरी किताब खो गई तो मैं परेशान हूं।"),
    ("next week we finished the audit", "Next week we will finish the audit."),
    ("tomorrow she joined the team", "Tomorrow she will join the team."),
    ("agle mahine hum naya office shift kiya", "Agle mahine hum naya office shift karenge."),
    ("अगले हफ्ते मीटिंग हुई थी", "अगले हफ्ते मीटिंग होगी।"),
    ("पिछले साल हम मनाली जाते हैं", "पिछले साल हम मनाली गए थे।"),
    ("रिपोर्ट में दो गलती है उन्हें ठीक कर दो", "रिपोर्ट में दो गलतियाँ हैं, उन्हें ठीक कर दो।"),
    ("kal raat main der se soya hoon isliye late hua", "Kal raat main der se soya tha, isliye late hua."),
    (
        "this issue is there from yesterday and nobody are looking at it",
        "This issue has been there since yesterday, and nobody is looking at it.",
    ),
    ("I am waiting for it from morning.", "I have been waiting for it since morning."),
    ("they are playing since morning at the terrace", "They have been playing since morning on the terrace."),
    ("i am working here since two years", "I have been working here for two years."),
    ("the retry wasteed the quota", "The retry wasted the quota."),
    (
        "on april fifth liam save twelve percent but his budget are off",
        "On April 5, Liam saved 12%, but his budget is off.",
    ),
    ("between you and i this is risky", "Between you and me, this is risky."),
    ("whom is coming to the party", "Who is coming to the party?"),
    ("less people came this time", "Fewer people came this time."),
]


@pytest.mark.parametrize(("said", "wrote"), GRAMMAR_ROUND_3)
def test_round_3_grammar_fixes_pass(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


@pytest.mark.parametrize(
    ("said", "wrote", "reason"),
    [
        # A verb that agreed with its noun ("meri gaadi ... gayi") is never made to disagree.
        ("meri gaadi kharab ho gayi", "Meri gaadi kharab ho gaya.", Reject.GENDER),
        ("मेरी गाड़ी खराब हो गई", "मेरी गाड़ी खराब हो गया।", Reject.GENDER),
        # A time word allows its own tense only.
        ("we finished the audit", "We will finish the audit.", Reject.UNHEARD),
        ("he did not go yesterday", "He went yesterday.", Reject.TOO_MANY_CHANGES),
        ("agle mahine hum shift karenge", "Agle mahine hum shift kiya.", Reject.UNHEARD),
        ("main kal aaunga", "Main kal aaungi.", Reject.GENDER),
    ],
)
def test_round_3_grammar_never_breaks_agreement_or_tense(said: str, wrote: str, reason: Reject) -> None:
    assert check(said, wrote, []) == reason


# Round 3: correction cues the guardrail did not know ("I meant", "oops", "nope", "correction", "mera matlab",
# "galti se bola", "रुको"), a lone "no" swapping a name at the start, "no not X", and an antonym said as the fix.
CORRECTIONS_ROUND_3 = [
    ("send it to anna sorry I meant maria", "Send it to Maria."),
    ("send it to anna oops to maria", "Send it to Maria."),
    ("send it to anna nope maria", "Send it to Maria."),
    ("anna ko bhejo mera matlab maria ko", "Maria ko bhejo."),
    ("anna ko bhejo galti se bola maria ko bhejo", "Maria ko bhejo."),
    ("anna ko bhejo ruko maria ko", "Maria ko bhejo."),
    ("अन्ना को भेजो मेरा मतलब मारिया को", "मारिया को भेजो।"),
    ("अन्ना को भेजो रुको मारिया को", "मारिया को भेजो।"),
    ("priya no rahul will lead the call", "Rahul will lead the call."),
    ("the meeting is on tuesday no not tuesday wednesday", "The meeting is on Wednesday."),
    ("the client is happy no unhappy with the draft", "The client is unhappy with the draft."),
    ("report finance ko bhejo nahi legal ko", "Report legal ko bhejo."),
    ("फाइल अमित को भेजो नहीं सुमित को", "फाइल सुमित को भेजो।"),
    ("maria sorry mariam ko call karo", "Mariam ko call karo."),
]


@pytest.mark.parametrize(("said", "wrote"), CORRECTIONS_ROUND_3)
def test_round_3_more_correction_cues_take_words_back(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        # The same words used plainly stay.
        ("the correction was filed on monday", "The was filed on Monday."),
        ("ruko yahan main abhi aata hoon", "Main abhi aata hoon."),
        ("galti se glass gir gaya", "Glass gir gaya."),
        ("i meant every word of it", "Every word of it."),
        ("priya will not lead the call", "Priya will lead the call."),
    ],
)
def test_round_3_plain_uses_of_cue_words_stay(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is not None


# Round 3: one content word left out with nothing written in its place is lost content, as a run of two is.
@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        (
            "my teacher asked for the draft by february fourteenth at five pm",
            "My asked for the draft by February 14 at 5 pm.",
        ),
        ("kevin wrote the introduction and rachel wrote the conclusion", "Kevin wrote the introduction, and wrote the"),
        (
            "please book the room for friday and send the agenda to the team",
            "Please book the room for Friday and send the agenda to the",
        ),
        ("client ko invoice kal bhejna hai", "Client ko kal bhejna hai."),
        ("मुझे रिपोर्ट कल तक भेजनी है", "मुझे कल तक भेजनी है।"),
        (
            "add a delay before the webhook fires then retry after ten minutes",
            "Add a delay before the webhook fires, 10 minutes.",
        ),
    ],
)
def test_round_3_a_single_dropped_content_word_is_refused(said: str, wrote: str) -> None:
    assert check(said, wrote, []) in (Reject.DROPPED_WORD, Reject.DROPPED_SENTENCE)


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        # Words moved, grammar words dropped and number words written as digits are no loss.
        ("so what workflows will be", "So, what will the workflows be?"),
        ("this section needs a more clearer hierarchy", "This section needs a clearer hierarchy."),
        ("the note says to discuss regarding the results", "The note says to discuss the results."),
        ("the fee is twenty euros", "The fee is €20."),
    ],
)
def test_round_3_moved_grammar_and_number_words_are_not_dropped(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


# Round 3: a dictionary term never recases a plain word the model kept plain ("cut me some slack" with Slack, "let
# us know" with US), nor a word inside an email, a web address or a path; a variant casing of the term is still put
# back ("NGINX" Nginx, "ios" iOS).
@pytest.mark.parametrize(
    ("text", "terms", "tidied"),
    [
        ("Cut me some slack, please.", ["Slack"], "Cut me some slack, please."),
        ("Can you zoom in?", ["Zoom"], "Can you zoom in?"),
        ("Let us know by Friday.", ["US"], "Let us know by Friday."),
        ("It is done.", ["IT"], "It is done."),
        (
            "I will mark it done and bill the client.",
            ["Will", "Mark", "Bill"],
            "I will mark it done and bill the client.",
        ),
        ("The teaser earned real cred.", ["CRED"], "The teaser earned real cred."),
        ("Thoda slack chahiye.", ["Slack"], "Thoda slack chahiye."),
        ("Mail sales@northstar.com today.", ["Northstar"], "Mail sales@northstar.com today."),
        ("Open portal.snapbak.app now.", ["Snapbak"], "Open portal.snapbak.app now."),
        ("Call /api/events first.", ["API"], "Call /api/events first."),
        ("Restart NGINX and the ios app.", ["Nginx", "iOS"], "Restart Nginx and the iOS app."),
        ("Pay with PayTM.", ["Paytm"], "Pay with Paytm."),
        ("Use node.js here.", ["Node.js"], "Use Node.js here."),
    ],
)
def test_round_3_tidy_recases_only_term_variants(text: str, terms: list[str], tidied: str) -> None:
    assert tidy(text, terms) == tidied


# Round 3: a direct question written as a statement: an English one that opens with an auxiliary and its subject or
# a question word and an auxiliary, and a Hindi or Hinglish one that opens or ends with "kya" or "क्या".
@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("what time is the standup tomorrow", "What time is the standup tomorrow."),
        ("can you send me the deck", "Can you send me the deck."),
        ("where is the invoice", "Where is the invoice."),
        ("kya report ready hai", "Kya report ready hai."),
        ("क्या रिपोर्ट तैयार है", "क्या रिपोर्ट तैयार है।"),
        ("प्लंबर आज आएगा क्या", "प्लंबर आज आएगा क्या।"),
        ("kya report ready hai", "Report ready hai."),
        ("क्या तुम आओगे", "क्या तुम आओगे؟"),
    ],
)
def test_round_3_a_question_written_as_a_statement_is_refused(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is not None


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        # An indirect question, an exclamation and a clause that opens with a question word stay statements.
        ("i wonder if the bank portal is slow today", "I wonder if the bank portal is slow today."),
        ("what a relief", "What a relief!"),
        ("when you get time call me", "When you get time, call me."),
        ("mujhe nahi pata kya hua", "Mujhe nahi pata kya hua."),
        ("pata karo ki reports kab ready hongi", "Pata karo ki reports kab ready hongi."),
        ("what i mean is we need more time", "What I mean is we need more time."),
    ],
)
def test_round_3_an_indirect_question_may_end_with_a_full_stop(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


# Round 3: an acknowledgement or sign-off the user never said ("Noted:", "Thanks!", a doubled "theek hai") is a
# reply, wherever it stands; said once, it may stay once.
@pytest.mark.parametrize(
    ("said", "wrote", "reason"),
    [
        (
            "sonia wants the invoice revised to ninety nine dollars not ninety dollars",
            "Noted: Sonia wants the invoice revised to $99, not $90.",
            Reject.REPLY,
        ),
        (
            "theek hai sir warmup ke baad squats start karenge",
            "Theek hai, theek hai, sir, warmup ke baad squats start karenge.",
            Reject.REPLY,
        ),
        (
            "your appointment is confirmed for thursday thank you for the forms",
            "Your appointment is confirmed for Thursday. Thank you for the forms. Thanks!",
            Reject.REPLY,
        ),
        ("ship the build today", "Done. Ship the build today.", Reject.REPLY),
        ("मीटिंग कल है", "ठीक है, मीटिंग कल है।", Reject.UNHEARD),
    ],
)
def test_round_3_an_unsaid_acknowledgement_is_a_reply(said: str, wrote: str, reason: Reject) -> None:
    assert check(said, wrote, []) == reason


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("okay ship it today", "Okay, ship it today."),
        ("theek hai sir kal milte hain", "Theek hai sir, kal milte hain."),
        ("send the report thanks", "Send the report. Thanks!"),
        ("is it done", "Is it done?"),
    ],
)
def test_round_3_an_acknowledgement_said_stays(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


# Round 3: "still", "already", "yet", "even", "again", "exactly", "abhi", "bhi" and "अभी" say when or how much.
@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("the ingress logs still show errors", "The ingress logs show errors."),
        ("we already paid the invoice", "We paid the invoice."),
        ("report abhi ready nahi hai", "Report ready nahi hai."),
        ("main bhi aaunga", "Main aaunga."),
    ],
)
def test_round_3_a_dropped_time_or_degree_word_is_a_qualifier(said: str, wrote: str) -> None:
    assert check(said, wrote, []) == Reject.QUALIFIER


def test_round_3_a_qualifier_heard_split_is_that_qualifier() -> None:
    assert check("the smoke test failed a gain", "The smoke test failed again.", []) is None


# Round 3: a correction of a number keeps only the new number; both versions kept around a cue are refused
# whether written as words or digits, or with a determiner before each; and an "or" or "and" never stands in
# for a dropped cue.
@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("the meeting is at three no four", "The meeting is at 3."),
        ("the price is fifty dollars wait sixty dollars", "The price is $50."),
        ("meeting teen baje hai matlab chaar baje", "Meeting 3 baje hai."),
        ("दो किलो चीनी लाना मतलब तीन किलो", "2 किलो चीनी लाना।"),
        ("the price is fifty dollars wait sixty dollars", "The price is $50, wait, $60."),
        ("the meeting is at three no four", "The meeting is at 3, no, 4."),
        ("call the plumber no the electrician", "Call the plumber, no, the electrician."),
        ("the meeting is at three no four", "The meeting is at 3 or 4."),
        (
            "we can include the case study actually include the testimonial",
            "We can include the case study and the testimonial.",
        ),
    ],
)
def test_round_3_a_correction_keeps_only_the_new_version(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is not None


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("the meeting is at three no four", "The meeting is at 4."),
        ("the price is fifty dollars wait sixty dollars", "The price is $60."),
        ("meeting teen baje hai matlab chaar baje", "Meeting 4 baje hai."),
        ("call the plumber no the electrician", "Call the electrician."),
        ("book twenty five seats no thirty", "Book 30 seats."),
    ],
)
def test_round_3_a_number_correction_writes_the_new_number(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


# Round 3 intake (b2): an ordinal written in digits ("12th") before a number cue read as no number and the guardrail
# crashed with an IndexError instead of judging the answer.
@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("we can send the plan by the 12th i mean the fourteenth", "We can send the plan by the 14th."),
        ("the review is on the 3rd no the fifth", "The review is on the 5th."),
    ],
)
def test_round_3_a_digit_ordinal_put_right_is_judged_without_crashing(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


def test_round_3_a_digit_ordinal_in_the_prior_put_right_in_the_next_piece_is_judged() -> None:
    prior = "Hi Ms. Rao,\n\nWe can send the revised floor plan by the 12th."
    revised = "Hi Ms. Rao,\n\nWe can send the revised floor plan by the 14th."
    assert judge(prior, "i mean the fourteenth", [], revised, "").rejected is None


# Round 3: right number and address formats the guardrail refused: a day said "the third of march", a range of
# hundreds, a decimal lakh or crore, a phone number's "plus", minutes "to" an hour, "point eight", English number
# words said in Devanagari, and an email whose labels are said in Devanagari.
@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("send it by the third of march", "Send it by March 3."),
        ("the logs were uploaded on the twenty second of november", "The logs were uploaded on November 22."),
        (
            "we will spend between four hundred and five hundred dollars on the stall",
            "We will spend between $400 and $500 on the stall.",
        ),
        ("one lakh fifty thousand rupees", "₹1.5 lakh."),
        ("ek crore bees lakh rupaye", "₹1.2 crore."),
        ("call me at plus nine one nine eight seven six five four three two one zero", "Call me at +91 98765 43210."),
        ("the call starts at ten to nine", "The call starts at 8:50."),
        (
            "the error rate dropped from two point four percent to point eight percent",
            "The error rate dropped from 2.4% to 0.8%.",
        ),
        ("सेक्शन ट्वेल्व पढ़ कर आना", "सेक्शन 12 पढ़ कर आना।"),
        ("बस नाइन थर्टी पर निकलेगी", "बस 9:30 पर निकलेगी।"),
        ("मेल करो नेहा एट जीमेल डॉट कॉम पर", "मेल करो neha@gmail.com पर।"),
        ("हमारे नॉलेज बेस का पता डब्ल्यू डब्ल्यू डब्ल्यू डॉट केबी डॉट सर्विसेज है", "हमारे नॉलेज बेस का पता www.kb.services है।"),
        ("एडमिन एट माइकंपनी डॉट आईओ पर लिखो", "admin@mycompany.io पर लिखो।"),
    ],
)
def test_round_3_dates_ranges_lakhs_phones_and_devanagari_addresses_pass(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("send it by the third of march", "Send it by March 4."),
        ("one lakh fifty thousand rupees", "₹1.6 lakh."),
        ("the call starts at ten to nine", "The call starts at 9:10."),
        ("call me at plus nine one nine eight seven", "Call me at +92 987."),
    ],
)
def test_round_3_a_wrong_date_amount_or_time_is_still_refused(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is not None


# Round 3: lists the speech never made: counts that say how many of a thing ("two engineers"), one item alone, a
# lead-in that announces nothing; and an answer that changes the order of what was said (list items, names).
@pytest.mark.parametrize(
    ("said", "wrote", "reason"),
    [
        (
            "we hired one designer two engineers and three interns",
            "We hired:\n1. Designer\n2. Engineers\n3. Interns",
            Reject.FALSE_LIST,
        ),
        ("ek chai do samose teen biscuit packet lao", "1. Chai\n2. Samose\n3. Biscuit packet lao", Reject.FALSE_LIST),
        (
            "my name is ravi i live in pune i like chess",
            "My name is Ravi:\n- I live in Pune\n- I like chess",
            Reject.FALSE_LIST,
        ),
        ("first we need to hire more people", "1. We need to hire more people", Reject.FALSE_LIST),
        (
            "i need three things first milk second eggs third bread",
            "I need three things:\n1. Milk\n2. Bread\n3. Eggs",
            Reject.REORDERED,
        ),
        ("book a train from paris to lyon", "Book a train from Lyon to Paris.", Reject.REORDERED),
        ("priya will send the invoice to rohan", "Rohan will send the invoice to Priya.", Reject.REORDERED),
    ],
)
def test_round_3_a_list_never_made_or_an_order_changed_is_refused(said: str, wrote: str, reason: Reject) -> None:
    assert check(said, wrote, []) == reason


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("One, milk. Two, eggs. Three, bread.", "1. Milk\n2. Eggs\n3. Bread"),
        (
            "ek, design final karo. do, pricing page banao. teen, sales ko brief karo.",
            "1. Design final karo\n2. Pricing page banao\n3. Sales ko brief karo",
        ),
        (
            "some notes for the offsite we leave friday the venue has a pool we return sunday",
            "Some notes for the offsite:\n- We leave Friday\n- The venue has a pool\n- We return Sunday",
        ),
        ("anna ko bhejo mera matlab maria ko", "Maria ko bhejo."),
        ("so what workflows will be", "So, what will the workflows be?"),
    ],
)
def test_round_3_counted_or_announced_lists_and_moved_grammar_pass(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


# Round 3: wrong formats the guardrail let through: a time, a currency, a decimal or a ratio changed, an address
# cut or changed, an "@" in prose, a spoken mark kept as words, a paragraph break said and dropped.
@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("meet on may twenty ninth at two pm", "Meet on May 29 at 9 pm."),
        ("the shop opens at quarter to five", "The shop opens at 4:15."),
        ("साढ़े चार बजे आना", "5:30 बजे आना।"),
        ("the fee is twenty euros", "The fee is $20."),
        ("the fee is twenty pounds", "The fee is ₹20."),
        ("growth was three point zero five percent", "Growth was 3.5%."),
        ("the ratio is three to one", "The ratio is 1:3."),
        ("mail priya dot sharma at acme dot co dot in", "Mail priya.sharma@acme.co."),
        ("mail priya underscore k at acme dot com", "Mail priya-k@acme.com."),
        ("open acme dot com slash pricing slash teams", "Open acme.com/teams/pricing."),
        ("we met at the office", "We met @ the office."),
        ("are you coming question mark", "Are you coming question mark?"),
        ("first line new paragraph second line", "First line. Second line."),
        ("पहली बात नया पैराग्राफ दूसरी बात", "पहली बात। दूसरी बात।"),
    ],
)
def test_round_3_a_changed_format_is_refused(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is not None


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("meet on may twenty ninth at two pm", "Meet on May 29 at 2 pm."),
        ("the shop opens at quarter to five", "The shop opens at 4:45."),
        ("the fee is twenty euros", "The fee is €20."),
        ("growth was three point zero five percent", "Growth was 3.05%."),
        ("the ratio is three to one", "The ratio is 3:1."),
        ("mail priya dot sharma at acme dot co dot in", "Mail priya.sharma@acme.co.in."),
        ("mail priya underscore k at acme dot com", "Mail priya_k@acme.com."),
        ("open acme dot com slash pricing slash teams", "Open acme.com/pricing/teams."),
        ("the endpoint is slash users slash id and it returns json", "The endpoint is /users/:id and it returns JSON."),
        ("first line new paragraph second line", "First line.\n\nSecond line."),
    ],
)
def test_round_3_the_right_format_passes(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


# Round 3: a sound-alike written where the right word was said: a grammar word after "the" ("the whether"), "except"
# where a verb goes ("will except"), a term after "the" or a subject ("the Lyft is broken", "I Reddit"); a Hindi
# question with a question word ending in a danda; a strong cue dropped with both versions kept.
@pytest.mark.parametrize(
    ("said", "wrote", "terms"),
    [
        ("the weather is nice today", "The whether is nice today.", []),
        ("i will accept the job", "I will except the job.", []),
        ("the lift is broken take the stairs", "The Lyft is broken, take the stairs.", ["Lyft"]),
        ("i read it yesterday", "I Reddit yesterday.", ["Reddit"]),
        ("मीटिंग कब है", "मीटिंग कब है।", []),
        ("approve it scratch that reject it", "Approve it. Reject it.", []),
    ],
)
def test_round_3_an_over_fix_a_lost_question_or_both_versions_are_refused(
    said: str, wrote: str, terms: list[str]
) -> None:
    assert check(said, wrote, terms) is not None


@pytest.mark.parametrize(
    ("said", "wrote", "terms"),
    [
        ("check weather it works", "Check whether it works.", []),
        ("please except the offer by friday", "Please accept the offer by Friday.", []),
        ("book a ride on lift tonight", "Book a ride on Lyft tonight.", ["Lyft"]),
        ("we pay invoices through zero", "We pay invoices through Xero.", ["Xero"]),
        ("ask procall for the quote", "Ask Procol for the quote.", ["Procol"]),
        ("approve it scratch that reject it", "Reject it.", []),
        ("pata nahi meeting kab hai", "Pata nahi meeting kab hai.", []),
        ("the travel in sure ance covers lost baggage", "The travel insurance covers lost baggage.", []),
        ("call shivon in lester", "Call Siobhan in Leicester.", ["Siobhan", "Leicester"]),
    ],
)
def test_round_3_a_fix_where_the_said_word_is_wrong_passes(said: str, wrote: str, terms: list[str]) -> None:
    assert check(said, wrote, terms) is None


# Round b1: a word the model respelled to another accepted spelling of the same word (a nukta, a chandrabindu, a
# Roman Hindi variant) is written as the speaker said it (rule 6), so it is never refused as dropped or unheard.
@pytest.mark.parametrize(
    ("said", "wrote", "written"),
    [
        (
            "kabir server load subah nine am se badh rha hai",
            "Kabir, server load subah 9 am se badh raha hai.",
            "Kabir, server load subah 9 am se badh rha hai.",
        ),
        (
            "amit uncle ki report aaj aa gai h",
            "Amit uncle ki report aaj aa gai hai.",
            "Amit uncle ki report aaj aa gai h.",
        ),
        ("acha didi notes bhejo", "Achha didi, notes bhejo.", "Acha didi, notes bhejo."),
        ("ok so we ship friday", "Okay, so we ship Friday.", "Ok, so we ship Friday."),
        ("मरीज की जांच के लिए कागज दिया", "मरीज़ की जाँच के लिए कागज़ दिया।", "मरीज की जांच के लिए कागज दिया।"),
        ("वो कल आयेगा और फाइल ले जायेगा", "वो कल आएगा और फ़ाइल ले जाएगा।", "वो कल आयेगा और फाइल ले जायेगा।"),
    ],
)
def test_round_b1_a_respelled_word_is_written_as_said(said: str, wrote: str, written: str) -> None:
    judged = judge("", said, [], None, wrote)
    assert judged.rejected is None
    assert judged.text == written


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("kal cake liye cake order karna hai", "Kal ke liye cake order karna hai."),
        ("hum log mall may milenge", "Hum log mall mein milenge."),
        ("रवि मेरी तारीफ से पूजा को धन्यवाद कह देना", "रवि, मेरी तरफ़ से पूजा को धन्यवाद कह देना।"),
    ],
)
def test_round_b1_a_misheard_word_fixed_is_not_put_back(said: str, wrote: str) -> None:
    judged = judge("", said, [], None, wrote)
    assert judged.rejected is None
    assert judged.text == wrote


# Round b1: a joining word that ends the whole speech with nothing after it ("... use it. But", "... है और") drops,
# and the sentence it hung on gets its full stop (founder, 11 Oct).
@pytest.mark.parametrize(
    ("said", "wrote", "written"),
    [
        ("कल सुबह जल्दी उठना है और", "कल सुबह जल्दी उठना है और", "कल सुबह जल्दी उठना है।"),
        (
            "मुझे ये काम करने हैं पहला दवाई लेनी है दूसरा डॉक्टर को फोन करना है। रिपोर्ट प्रिंट करनी है। तो",
            "मुझे ये काम करने हैं:\n1. दवाई लेनी है\n2. डॉक्टर को फोन करना है\n3. रिपोर्ट प्रिंट करनी है\nतो",
            "मुझे ये काम करने हैं:\n1. दवाई लेनी है\n2. डॉक्टर को फोन करना है\n3. रिपोर्ट प्रिंट करनी है",
        ),
        (
            "pack the charger and the cables. And",
            "Pack the charger and the cables. And",
            "Pack the charger and the cables.",
        ),
        (
            "send the invoice to the client today. So",
            "Send the invoice to the client today. So",
            "Send the invoice to the client today.",
        ),
    ],
)
def test_round_b1_a_dangling_final_joining_word_drops(said: str, wrote: str, written: str) -> None:
    judged = judge("", said, [], None, wrote)
    assert judged.rejected is None
    assert judged.text == written


# "so", "then", "phir" and "तो" end a sentence too ("I told him so", "kal milte hain phir"): only after a full stop
# are they left dangling.
@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("salt and pepper", "Salt and pepper."),
        ("i told him so", "I told him so."),
        ("kal milte hain phir", "Kal milte hain phir."),
        ("who are you sending it to", "Who are you sending it to?"),
        ("their team will call us then", "Their team will call us then."),
    ],
)
def test_round_b1_a_word_that_ends_a_sentence_stays(said: str, wrote: str) -> None:
    judged = judge("", said, [], None, wrote)
    assert judged.text == wrote


# Round b1: raw Devanagari of four words or more, typed back with no mark at all, was never polished.
def test_round_b1_raw_devanagari_is_unpolished() -> None:
    said = "एक काम करो मुझे एक प्रस्ताव लिखना है क्लाइंट के लिए तो उसमें क्या होगा"
    judged = judge("", said, [], None, said)
    assert judged.rejected == Reject.UNPOLISHED


# Round b1: "kitna" before a word of praise, or after an interjection, exclaims; a full stop or "!" is right.
@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("कितनी प्यारी बच्ची है", "कितनी प्यारी बच्ची है।"),
        ("kitna pyara ghar hai", "Kitna pyara ghar hai."),
        ("order aa gaya wah packing kitni premium lagi", "Order aa gaya. Wah, packing kitni premium lagi."),
    ],
)
def test_round_b1_an_exclamation_may_end_with_a_full_stop(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


def test_round_b1_a_kitna_question_still_needs_its_question_mark() -> None:
    assert check("kitne log aaye hain", "Kitne log aaye hain.", []) == Reject.QUESTION


# Round b1: "slash" said as a verb after its subject or a modal is never a path's "/".
@pytest.mark.parametrize(
    ("said", "wrote"),
    [("we slash cloud spend", "We /cloud spend."), ("diego should slash replicas", "Diego should /replicas.")],
)
def test_round_b1_slash_as_a_verb_is_never_a_path(said: str, wrote: str) -> None:
    assert check(said, wrote, []) == Reject.FORMAT


# Round b2 fix (11 Oct): an accepted answer's list and marks are put in shape without a model: a bullet after
# numbered items takes the next number, a first item glued to the line before goes on its own line, the words
# before a list end with a colon, counting words left in a numbered item go, a "?" the speaker said on an item or
# the piece's end comes back, and a "." after Devanagari ends a sentence as "।".
@pytest.mark.parametrize(
    ("prior", "said", "wrote", "shaped"),
    [
        (
            "",
            "Okay i need to do the following first turn off the tv second Turn off the lamps. Keep my laptop in a "
            "place where I cannot use it. But",
            "Okay, I need to do the following:\n1. Turn off the TV\n2. Turn off the lamps\n- Keep my laptop in a "
            "place where I cannot use it.",
            "Okay, I need to do the following:\n1. Turn off the TV\n2. Turn off the lamps\n3. Keep my laptop in a "
            "place where I cannot use it.",
        ),
        (
            "",
            "Ok, so this is just a simple test that I am running. First it needs to be an audit set second I want "
            "to buy a chair. Third lets get eggs for the breakfast. It's it's finished finished.",
            "Ok, so this is just a simple test that I am running. 1. It needs to be an audit set\n2. I want to buy "
            "a chair\n3. Let's get eggs for the breakfast\nIt's finished.",
            "Ok, so this is just a simple test that I am running:\n1. It needs to be an audit set\n2. I want to buy "
            "a chair\n3. Let's get eggs for the breakfast\nIt's finished.",
        ),
        (
            "",
            "we packed three bags first the tent second the stove third the food",
            "We packed three bags.\n1. The tent\n2. The stove\n3. The food",
            "We packed three bags:\n1. The tent\n2. The stove\n3. The food",
        ),
        (
            "",
            "I want to know a few things. First, can you tell me how it works? Second, can I add this? Is over "
            "here. Third, how well will it perform with the long text?",
            "I want to know a few things:\n1. First, can you tell me how it works?\n2. Second, can I add this? It's "
            "over here\n3. Third, how well will it perform with the long text?",
            "I want to know a few things:\n1. Can you tell me how it works?\n2. Can I add this? It's over here\n3. "
            "How well will it perform with the long text?",
        ),
        (
            "So these are the things that I need to test:\n1. Is it going to show bullet points\n2. How well is it "
            "going to look",
            "and How is it going to show me the data?",
            "and how is it going to show me the data",
            "and how is it going to show me the data?",
        ),
        (
            "मुझे ये देखना है:\n1. लिस्ट बुलेट में आती है या नहीं\n2. कितना अच्छा दिखता है",
            "और डेटा कैसे दिखाता है?",
            "और डेटा कैसे दिखाता है",
            "और डेटा कैसे दिखाता है?",
        ),
        ("", "कल मीटिंग है. रिपोर्ट भेजो", "कल मीटिंग है. रिपोर्ट भेजो।", "कल मीटिंग है। रिपोर्ट भेजो।"),
        ("", "डॉ. शर्मा कल आएंगे", "डॉ. शर्मा कल आएंगे।", "डॉ. शर्मा कल आएंगे।"),
    ],
)
def test_round_b2_an_accepted_answer_is_put_in_shape(prior: str, said: str, wrote: str, shaped: str) -> None:
    judged = judge(prior, said, [], None, wrote)
    assert judged.rejected is None
    assert judged.text == shaped


@pytest.mark.parametrize(
    "wrote",
    [
        "We need:\n1. पहली लाइन में कोड लिखो\n2. दूसरी लाइन में दाम",
        "Steps:\n1. First aid kit\n2. Second hand books",
        "Is this right?\n- Yes",
        "Done.\n\nNext we eat.",
    ],
)
def test_round_b2_shaping_leaves_a_right_answer_alone(wrote: str) -> None:
    from snapbak_backend.ai.guardrail.shape import shaped

    assert shaped("", wrote) == wrote


def test_round_b2_a_refused_piece_keeps_its_joining_word_in_lowercase() -> None:
    prior = "So these are the things:\n1. Is it fast\n2. How well is it going to look"
    judged = judge(
        prior, "and How is it going to show me the data?", [], None, "And how is it going to show me the charts?"
    )
    assert judged.rejected == Reject.UNHEARD
    assert judged.text == "and how is it going to show me the data?"


# Round b2 fix: two refusals put right without a model. A piece going on with a sentence that came back as a new list
# item or without its joining word gets the word back, in lowercase; a paragraph said but run on gets its blank
# line after the sentence the speaker said it after.
@pytest.mark.parametrize(
    ("prior", "said", "wrote", "fixed"),
    [
        (
            "So these are the things:\n1. Is it fast\n2. How well is it going to look",
            "and How is it going to show me the data?",
            "3. how is it going to show me the data?",
            "and how is it going to show me the data?",
        ),
        (
            "So these are the things:\n1. Is it fast\n2. How well is it going to look",
            "and How is it going to show me the data?",
            "3. and how is it going to show me the data?",
            "and how is it going to show me the data?",
        ),
        (
            "मुझे ये देखना है:\n1. लिस्ट बुलेट में आती है या नहीं\n2. कितना अच्छा दिखता है",
            "और डेटा कैसे दिखाता है?",
            "3. डेटा कैसे दिखाता है?",
            "और डेटा कैसे दिखाता है?",
        ),
        (
            "",
            "the trip was great new paragraph next year we want to go to japan",
            "The trip was great.\nNext year we want to go to Japan.",
            "The trip was great.\n\nNext year we want to go to Japan.",
        ),
        # Round b5: a run-on split by dropping its joining word gets the word back opening the sentence.
        (
            "",
            "it will be a simple queue and it will pick the next job",
            "It will be a simple queue. It will pick the next job.",
            "It will be a simple queue. And it will pick the next job.",
        ),
        (
            "",
            "मीटिंग चार बजे होगी और नोट्स कल आएंगे",
            "मीटिंग 4 बजे होगी। नोट्स कल आएंगे।",
            "मीटिंग 4 बजे होगी। और नोट्स कल आएंगे।",
        ),
        (
            "",
            "we met the client today and they liked it new paragraph the next call is on friday",
            "We met the client today, and they liked it. The next call is on Friday.",
            "We met the client today, and they liked it.\n\nThe next call is on Friday.",
        ),
    ],
)
def test_round_b2_a_refusal_with_a_known_repair_is_put_right(prior: str, said: str, wrote: str, fixed: str) -> None:
    from snapbak_backend.schemas.polish import PolishRequest
    from snapbak_backend.transformers.polish import fold, read

    fields = {
        "text": said,
        "prior": prior,
        "terms": [],
        "app": "",
        "style": "chat",
        "language": "en",
        "deadline_ms": 20000,
    }
    request = PolishRequest.model_validate(fields)
    assert judge(prior, said, [], None, wrote).rejected is not None
    judged = read(request, fold(request), json.dumps({"revision": None, "text": wrote}))
    assert judged.rejected is None
    assert judged.text == fixed


# Round b2 fix: refused now. A short dictation's "phir", "bhi" or "then" dropped; a spoken number written as its word
# and its digit ("two 2-liter"); an email with Devanagari in it; a fraction word left before an hour's digit ("saade
# 6"); a revision that changes the prior's words though the piece takes nothing back.
@pytest.mark.parametrize(
    ("said", "wrote", "rule"),
    [
        ("kal milte hain phir", "Kal milte hain.", Reject.DROPPED_WORD),
        ("main bhi aaunga", "Main aaunga.", Reject.QUALIFIER),
        ("see you then", "See you.", Reject.DROPPED_WORD),
        ("buy two liter bottles of oil", "Buy two 2-liter bottles of oil.", Reject.UNHEARD),
        ("ईमेल राहुल एट मीरा डॉट इन", "ईमेल राहुल@मीरा.इन", Reject.FORMAT),
        ("sava chhe baje gym chalenge", "Sava 6 baje gym chalenge.", Reject.FORMAT),
        ("बारात साढ़े छह बजे पहुंचेगी", "बारात साढ़े 6 बजे पहुंचेगी।", Reject.FORMAT),
    ],
)
def test_round_b2_a_wrong_answer_is_refused(said: str, wrote: str, rule: Reject) -> None:
    assert check(said, wrote, []) == rule


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("kal milte hain phir", "Kal milte hain phir."),
        ("we left. and", "We left."),
        ("buy two liter bottles of oil", "Buy 2 liter bottles of oil."),
        ("sava chhe baje gym chalenge", "6:15 baje gym chalenge."),
        ("बारात साढ़े छह बजे पहुंचेगी", "बारात साढ़े छह बजे पहुंचेगी।"),
        ("mail rahul at meera dot in", "Mail rahul@meera.in."),
    ],
)
def test_round_b2_a_right_answer_is_still_accepted(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


# Round b2 fix: a blank line the speaker never asked for runs on, unless it sets off an email's greeting or sign-off;
# before a list it is one line break.
@pytest.mark.parametrize(
    ("said", "wrote", "shaped"),
    [
        (
            "hi my name is meera here are a few things about me i paint i cook",
            "Hi, my name is Meera.\n\nHere are a few things about me:\n- I paint\n- I cook",
            "Hi, my name is Meera. Here are a few things about me:\n- I paint\n- I cook",
        ),
        (
            "here are a few things about me i paint i cook",
            "Here are a few things about me:\n\n- I paint\n- I cook",
            "Here are a few things about me:\n- I paint\n- I cook",
        ),
        (
            "hi sam the deck is ready thanks meera",
            "Hi Sam,\n\nThe deck is ready.\n\nThanks,\nMeera",
            "Hi Sam,\n\nThe deck is ready.\n\nThanks,\nMeera",
        ),
        (
            "the trip was great new paragraph next year we go to japan",
            "The trip was great.\n\nNext year we go to Japan.",
            "The trip was great.\n\nNext year we go to Japan.",
        ),
    ],
)
def test_round_b2_an_unasked_paragraph_break_runs_on(said: str, wrote: str, shaped: str) -> None:
    from snapbak_backend.ai.guardrail.shape import shaped as shape

    assert shape(said, wrote) == shaped


def test_round_b2_an_accepted_piece_lowercases_a_capital_the_recognizer_put_after_its_joining_word() -> None:
    prior = "So these are the things:\n1. Is it fast\n2. How well is it going to look"
    said = "and How is it going to show me the data?"
    judged = judge(prior, said, [], None, said)
    assert judged.text == "and how is it going to show me the data?"


def test_round_b3_a_half_converted_clock_fraction_is_named_not_every_number() -> None:
    # A long note with one "paune 8 baje" was refused naming all 22 numbers, so the correction could not fix it.
    said = (
        "subah chhe baje uth gayi aur saat baje walk ki trainer ne bola paune aath baje tak dinner aur raat nau baje "
        "so jao"
    )
    wrote = "Subah 6 baje uth gayi aur 7 baje walk ki. Trainer ne bola paune 8 baje tak dinner aur raat 9 baje so jao."
    assert verdict(said, wrote, []) == Reason(Reject.FORMAT, ("paune 8",))
    assert verdict(said, wrote.replace("paune 8", "7:45"), []) is None


def test_round_b3_a_number_out_of_order_names_only_that_number() -> None:
    found = verdict("send fifteen boxes and twenty bags today", "Send 20 boxes and 15 bags today.", [])
    assert found == Reason(Reject.UNHEARD, ("15",))


def test_round_b3_a_correction_that_says_the_negated_verb_again_takes_the_negation_back() -> None:
    # backtracking-500: the cue takes back the whole "don't send ..." clause by saying "send" again.
    prior = "don't send the invoice to the client yet"
    accepted = judge(prior, "no wait send it today", [], "", "Send the invoice to the client today.")
    assert accepted.rejected is None
    # A correction that restates only the object keeps the negation.
    kept = verdict("don't send it to Sam no wait to Ravi", "Send it to Ravi.", [])
    assert kept is not None and kept.rule == Reject.NEGATION


@pytest.mark.parametrize(
    ("said", "wrote", "shaped"),
    [
        ("मकान मालिक ने तारीख पूछी?", "मकान मालिक ने तारीख पूछी।?", "मकान मालिक ने तारीख पूछी?"),
        (
            "Tom asked colon are we keeping the launch on Friday question mark period",
            "Tom asked: Are we keeping the launch on Friday?.",
            "Tom asked: Are we keeping the launch on Friday?",
        ),
        ("wait for it", "Wait for it...", "Wait for it..."),
        (
            "hi ana the invoice is ready cheers tom",
            "Hi Ana,\n\nThe invoice is ready.\n\nCheers,\nTom.",
            "Hi Ana,\n\nThe invoice is ready.\n\nCheers,\nTom",
        ),
    ],
)
def test_round_b3_stacked_end_marks_collapse_and_a_sign_off_name_takes_no_full_stop(
    said: str, wrote: str, shaped: str
) -> None:
    assert judge("", said, [], None, wrote).text == shaped


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("is mahine bujurg logon ke liye lift band rahegi", "Is mahine buzurg logon ke liye lift band rahegi."),
        ("mujhe jyada time chahiye", "Mujhe zyada time chahiye."),
    ],
)
def test_round_b3_a_roman_hindi_j_or_z_spelling_goes_back_to_the_speakers(said: str, wrote: str) -> None:
    # suite-growth-hinglish-02192: a five-paragraph email was AI-skipped for "bujurg" written "Buzurg".
    judged = judge("", said, [], None, wrote)
    assert judged.rejected is None
    assert judged.text == wrote.replace("buzurg", "bujurg").replace("zyada", "jyada")


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        # suite-growth-hinglish-05022: a sound-alike written in its plural is still the sound-alike.
        ("car ke break thode tight karwa dena service me", "Car ke brakes thode tight karwa dena service me."),
        # v2-grammar-hinglish-00045: a compound written with a hyphen sounds as its words joined.
        ("brake glass access use nahi hua", "Break-glass access use nahi hua."),
    ],
)
def test_round_b4_a_sound_alike_may_take_a_plural_or_a_hyphen(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


def test_round_b4_a_restated_sentence_may_keep_the_nouns_its_pronouns_stand_for() -> None:
    # backtracking-386: "give it to her" after the cue restates "give the medicine to Grandma"; the gold's own answer
    # keeps the nouns and the new time, and was refused twice, so the raw transcript shipped.
    said = "Give the medicine to Grandma after lunch, scratch that, give it to her before lunch."
    assert check(said, "Give the medicine to Grandma before lunch.", []) is None
    assert check(said, "Give it to her before lunch.", []) is None
    assert check(said, "Give the medicine to Grandma after lunch.", []) is not None


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        # suite-growth-en-00375: a name said once comes back as an invented sign-off.
        (
            "hi ms patel my son aarav was absent on tuesday thank you for your help",
            "Hi Ms. Patel,\n\nMy son Aarav was absent on Tuesday. Thank you for your help.\n\nAarav",
        ),
        # v2-backtracking-en-00013: a count said once is written twice.
        ("please include two liter bottles of olive oil", "Please include two two-liter bottles of olive oil."),
    ],
)
def test_round_b4_a_name_or_number_written_more_often_than_said_is_unheard(said: str, wrote: str) -> None:
    assert check(said, wrote, []) == Reject.UNHEARD


def test_round_b4_a_question_mark_inside_a_closing_quote_takes_no_full_stop_after_it() -> None:
    # v2-numbers-en-00323: "?”." stacks two end marks.
    said = "Joaquin asked should I keep the old one"
    judged = judge("", said, [], None, "Joaquin asked, “Should I keep the old one?”.")
    assert judged.text == "Joaquin asked, “Should I keep the old one?”"


def test_round_b4_a_piece_with_no_correction_leaves_the_prior_as_typed() -> None:
    # founder-016: the prior was already typed; a piece that takes nothing back may not rewrite it.
    prior = "Things to test:\n1. Is it going to present the list in bullet points\n2. How well is it going to look"
    judged = judge(
        prior,
        "and how is it going to show me the data?",
        [],
        prior.replace("points", "points?"),
        "and how is it going to show me the data?",
    )
    assert judged.revision is None
    assert judged.rejected is None


def test_round_b4_too_many_changes_names_the_words_that_cost_it() -> None:
    # v2-backtracking-hi-00113: the refusal named number words written as digits, not the moved "एक test", so the
    # retry could not tell what to put back.
    said = "मैं ने कल एक test expired token के लिए डाला और फिर रिपोर्ट भेजी"
    wrote = "मैंने कल expired token के लिए एक test डाला, और फिर रिपोर्ट भेजी।"
    reason = verdict(said, wrote, [])
    assert reason is not None
    assert "एक" in reason.words or "test" in reason.words


ANNOUNCED = "Hi, I am Ravi. Here are a few things about my team. We ship on Fridays. We review code daily."


@pytest.mark.parametrize(
    ("said", "wrote", "rule"),
    [
        # founder-019, round b4: the recognizer's own stops after an announcement kept the statements as prose in
        # every run (D5).
        (ANNOUNCED, ANNOUNCED, Reject.UNLISTED),
        (
            ANNOUNCED,
            "Hi, I am Ravi. Here are a few things about my team:\n- We ship on Fridays\n- We review code daily",
            None,
        ),
        # One statement after it, or speech with no stops to count sentences by, is left to the model.
        (
            "Here are a few things about my team. We ship on Fridays.",
            "Here are a few things about my team. We ship on Fridays.",
            None,
        ),
        (
            "here are a few things about my team we ship on fridays we review code daily",
            "Here are a few things about my team. We ship on Fridays. We review code daily.",
            None,
        ),
        # Questions after it are asked, never listed.
        (
            "Here are a few things about the trip. Can you book it? Is it cheap?",
            "Here are a few things about the trip. Can you book it? Is it cheap?",
            None,
        ),
    ],
)
def test_round_b4_announced_statements_the_recognizer_stopped_are_a_list(
    said: str, wrote: str, rule: Reject | None
) -> None:
    assert check(said, wrote, []) == rule


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        # v2-numbers-en-00242: a decimal's two digits are one number, not a count written twice.
        (
            "keep the pipe length between two point two and two point four meters",
            "Keep the pipe length between 2.2 and 2.4 meters.",
        ),
        # A count that ends one sentence and opens the next was said twice.
        ("we need two two of them are broken", "We need two. Two of them are broken."),
    ],
)
def test_round_b4_a_count_beside_a_mark_is_not_written_twice(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


@pytest.mark.parametrize(
    ("prior", "text", "revision"),
    [
        ("Should we meet on Friday", "question mark", "Should we meet on Friday?"),
        ("We met on Friday", "full stop", "We met on Friday."),
    ],
)
def test_round_b4_a_piece_that_says_a_mark_may_put_it_on_the_prior(prior: str, text: str, revision: str) -> None:
    judged = judge(prior, text, [], revision, "")
    assert judged.rejected is None
    assert judged.revision == revision


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        # Round b5 (founder-022, v2-stt-nearsound-hi-00198): a run-on split into sentences lost its spoken "and".
        (
            "it will be a simple queue and it will pick the next job",
            "It will be a simple queue. It will pick the next job.",
        ),
        ("server down tha aur sab log wait kar rahe the", "Server down tha. Sab log wait kar rahe the."),
        ("मीटिंग चार बजे होगी और नोट्स कल आएंगे", "मीटिंग 4 बजे होगी। नोट्स कल आएंगे।"),
        ("the build failed so we rolled back", "The build failed. We rolled back."),
    ],
)
def test_round_b5_a_joining_word_dropped_at_a_sentence_split_is_refused(said: str, wrote: str) -> None:
    reason = verdict(said, wrote, [])
    assert reason is not None
    assert reason.rule == Reject.DROPPED_WORD


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("server down tha aur sab log wait kar rahe the", "Server down tha, aur sab log wait kar rahe the."),
        ("server down tha aur sab log wait kar rahe the", "Server down tha. Aur sab log wait kar rahe the."),
        (
            "it will be a simple queue and it will pick the next job",
            "It will be a simple queue, and it will pick the next job.",
        ),
        # A joining word a correction takes back goes with it, and one said twice in a row is a stutter (founder-022).
        ("book the hall and no wait book the room", "Book the room."),
        ("it should run and And it should stop", "It should run. And it should stop."),
    ],
)
def test_round_b5_a_joining_word_kept_at_a_sentence_split_is_accepted(said: str, wrote: str) -> None:
    assert check(said, wrote, []) is None


def test_round_b5_words_put_on_lines_of_their_own_with_no_list_asked_are_a_false_list() -> None:
    # v2-punctuation-en-00034: a four-word run-on came back one word per line.
    assert check("tom cooks lisa cleans", "Tom\nCooks\nLisa\nCleans", []) == Reject.FALSE_LIST


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("milk new line eggs new line bread", "Milk\nEggs\nBread"),
        ("hi sam the deck is ready thanks ravi", "Hi Sam,\n\nThe deck is ready.\n\nThanks,\nRavi"),
    ],
)
def test_round_b5_lines_said_or_an_email_shape_are_no_false_list(said: str, wrote: str) -> None:
    assert check(said, wrote, []) != Reject.FALSE_LIST


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        # v2-numbers-hi-00069: one spoken "क्या" ended one question and opened the next.
        (
            "driver nau baje pahunchega kya usse advance mil gaya",
            "Driver 9 baje pahunchega kya? Kya usse advance mil gaya?",
        ),
        ("ड्राइवर नौ बजे पहुंचेगा क्या उसे एडवांस मिल गया", "ड्राइवर 9 बजे पहुंचेगा क्या? क्या उसे एडवांस मिल गया?"),
    ],
)
def test_round_b5_a_question_word_written_more_often_than_said_is_refused(said: str, wrote: str) -> None:
    assert check(said, wrote, []) == Reject.REPEATED


def test_round_b5_a_question_word_said_twice_may_be_written_twice() -> None:
    said = "driver pahunchega kya kya usse advance mila"
    assert check(said, "Driver pahunchega kya? Kya usse advance mila?", []) != Reject.REPEATED


def test_round_b5_too_many_changes_names_no_number_written_as_digits() -> None:
    # v2-backtracking-hi-00148: the refusal named every number word written as digits, hiding the moved words.
    said = "send the twelve invoices to priya and the three receipts to amit by friday"
    reason = verdict(said, "Send priya 12 invoices and amit 3 receipts by Friday.", [])
    assert reason is not None
    assert reason.rule == Reject.TOO_MANY_CHANGES
    assert {"twelve", "three"}.isdisjoint(reason.words)
    assert "invoices" in reason.words


HINDI_LIST = "मुझे ये देखना है:\n1. लिस्ट बुलेट में आती है या नहीं\n2. कितना अच्छा दिखता है"
ROMAN_LIST = "Mujhe ye check karna hai:\n1. List bullet points mein aati hai ya nahi\n2. Kitna accha dikhta hai"


@pytest.mark.parametrize(
    ("prior", "text", "answer", "joined"),
    [
        # founder-018, round b5: a piece with no correction came back as the prior with the piece added as an item.
        (
            HINDI_LIST,
            "और डेटा कैसे दिखाता है?",
            {"revision": HINDI_LIST + "\n3. और डेटा कैसे दिखाता है?", "text": ""},
            HINDI_LIST + " और डेटा कैसे दिखाता है?",
        ),
        # founder-017: the prior re-marked with the piece run into it, and the prior re-marked with the piece as a
        # new item; the typed prior stays and the piece goes on with its last item.
        (
            ROMAN_LIST,
            "Aur Data kaise dikhata hai?",
            {"revision": ROMAN_LIST.replace("nahi", "nahi?") + " aur data kaise dikhata hai?", "text": ""},
            ROMAN_LIST + " aur data kaise dikhata hai?",
        ),
        (
            ROMAN_LIST,
            "Aur Data kaise dikhata hai?",
            {"revision": ROMAN_LIST.replace("nahi", "nahi?") + "?", "text": "3. Data kaise dikhata hai?"},
            ROMAN_LIST + " aur Data kaise dikhata hai?",
        ),
    ],
)
def test_round_b5_a_piece_with_no_correction_never_rewrites_the_typed_prior(
    prior: str, text: str, answer: dict[str, str], joined: str
) -> None:
    from snapbak_backend.schemas.polish import PolishRequest
    from snapbak_backend.transformers.polish import fold, read

    fields = {"text": text, "prior": prior, "terms": [], "app": "", "style": "chat", "language": "hi"}
    request = PolishRequest.model_validate({**fields, "deadline_ms": 20000})
    polished = read(request, fold(request), json.dumps(answer, ensure_ascii=False))
    assert polished.rejected is None
    assert polished.revision is None
    assert f"{prior} {polished.text}" == joined


def read_answer(said: str, wrote: str, terms: list[str] | None = None, prior: str = "") -> Polished:
    from snapbak_backend.schemas.polish import PolishRequest
    from snapbak_backend.transformers.polish import fold, read

    fields = {"text": said, "prior": prior, "terms": terms or [], "app": "", "style": "chat", "language": "en"}
    request = PolishRequest.model_validate({**fields, "deadline_ms": 20000})
    answer = json.dumps({"revision": None, "text": wrote}, ensure_ascii=False)
    return read(request, fold(request), answer)


def test_round_b6_a_question_mark_said_after_a_danda_replaces_it() -> None:
    # v2-punctuation-hi-00070: the "?" said came back after the model's "।" ("पूछी।?").
    said = "मकान मालिक ने किराया जमा करने की तारीख पूछी?"
    assert judge("", said, [], None, "मकान मालिक ने किराया जमा करने की तारीख पूछी।").text == said


def test_round_b6_a_direct_question_left_with_a_full_stop_is_mended_without_a_model() -> None:
    # formatting-214: the first answer had every word right and only lacked its "?"; the correction echoed the
    # checker's note and the text went in as said.
    said = "can you polish this for ethan and mention the discount applies for five days only"
    wrote = "Can you polish this for Ethan and mention the discount applies for 5 days only."
    polished = read_answer(said, wrote, ["Ethan"])
    assert (polished.rejected, polished.text) == (None, wrote[:-1] + "?")


def test_round_b6_announced_statements_written_as_prose_are_listed_without_a_model() -> None:
    # founder-019 run 2: the words were right (Saksham, Procol) and only the D5 list was missing.
    said = (
        "Hi, my name is Aksham. Here are a few things about me. I'm a 27-year-old software engineer. I work at a "
        "company called Procall. I am leaving Procall on Monday and I'll be joining a new company called Hello Council."
    )
    wrote = (
        "Hi, my name is Saksham. Here are a few things about me. I'm a 27-year-old software engineer. I work at a "
        "company called Procol. I am leaving Procol on Monday and I'll be joining a new company called Hello Council."
    )
    listed = (
        "Hi, my name is Saksham. Here are a few things about me:\n- I'm a 27-year-old software engineer\n"
        "- I work at a company called Procol\n"
        "- I am leaving Procol on Monday and I'll be joining a new company called Hello Council"
    )
    polished = read_answer(said, wrote, ["Procol", "Saksham"])
    assert (polished.rejected, polished.text) == (None, listed)


def test_round_b6_a_list_counted_off_but_bulleted_is_numbered_without_a_model() -> None:
    said = "first call the bank second pay the rent third book the cab"
    polished = read_answer(said, "- Call the bank\n- Pay the rent\n- Book the cab")
    assert (polished.rejected, polished.text) == (None, "1. Call the bank\n2. Pay the rent\n3. Book the cab")


def test_round_b6_a_mended_answer_that_breaks_another_rule_stays_refused() -> None:
    said = "can you polish this for ethan"
    polished = read_answer(said, "Can you polish this for Ethan today.", ["Ethan"])
    assert polished.rejected == Reject.UNHEARD


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        # v2-lists-en-00018: "do one thing," then two steps joined by "then" stays one sentence.
        (
            "Dad please check Nia's backpack before dinner, her library card is probably in the side pocket. Do one "
            "thing, read the chapter with her, then sign the reading log. The teacher wants it on the blue folder",
            "Dad, please check Nia's backpack before dinner. Her library card is probably in the side pocket. Do one "
            "thing:\n- Read the chapter with her\n- Sign the reading log\nThe teacher wants it on the blue folder.",
        ),
        (
            "ek kaam karo, report bhejo phir usko call karo",
            "Ek kaam karo:\n- Report bhejo\n- Usko call karo",
        ),
    ],
)
def test_round_b6_two_steps_after_do_one_thing_with_a_comma_are_no_list(said: str, wrote: str) -> None:
    found = verdict(said, wrote, [])
    assert found is not None and found.rule == Reject.FALSE_LIST


@pytest.mark.parametrize(
    ("said", "terms", "wrote", "kept"),
    [
        # suite-growth-en-05292 and -03338: a term that is also a plain word, after a describing word.
        (
            "please pay the electricity bill before friday",
            ["Bill"],
            "Please pay the electricity Bill before Friday.",
            "Please pay the electricity bill before Friday.",
        ),
        ("this yoga asana is hard on the knees", ["Asana"], "This yoga Asana is hard on the knees.", None),
        # Where the term names the thing, it keeps its capital.
        ("ask bill to send the invoice", ["Bill"], "Ask Bill to send the invoice.", "Ask Bill to send the invoice."),
        ("put the task in asana", ["Asana"], "Put the task in Asana.", "Put the task in Asana."),
    ],
)
def test_round_b6_a_term_twin_after_a_describing_word_stays_the_plain_word(
    said: str, terms: list[str], wrote: str, kept: str | None
) -> None:
    polished = judge("", said, terms, None, wrote)
    assert polished.rejected is None
    assert polished.text == (kept or "This yoga asana is hard on the knees.")


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        # v2-stt-homophones-en-00006 and v2-punctuation-en-00014: a short dictation broken onto lines.
        ("signed buy Patel", "Signed by\nPatel"),
        ("thanks proposal attached", "Thanks.\n\nProposal attached."),
    ],
)
def test_round_b6_a_short_dictation_gets_no_line_break_never_said(said: str, wrote: str) -> None:
    assert judge("", said, [], None, wrote).rejected == Reject.FALSE_LIST


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("hi sam see you tomorrow", "Hi Sam,\nSee you tomorrow."),
        ("thanks a lot cheers tom", "Thanks a lot.\n\nCheers,\nTom"),
    ],
)
def test_round_b6_a_short_greeting_or_sign_off_keeps_its_line(said: str, wrote: str) -> None:
    assert judge("", said, [], None, wrote).text == wrote


def test_round_b6_an_address_left_half_spoken_is_refused() -> None:
    # formatting-070: "dot" became "." but "at" stayed a word.
    found = verdict(
        "Can Priya send the revised agenda to Sam at acme dot io question mark",
        "Can Priya send the revised agenda to Sam at acme.io?",
        [],
    )
    assert found is not None and found.rule == Reject.FORMAT


@pytest.mark.parametrize(
    ("prior", "text", "wrote"),
    [
        # v2-backtracking-en-00114, v2-backtracking-hi-00151, hinglish-backtracking-043: a piece that opens with a
        # cue, answered with the cue still between the old and the new version.
        (
            "doctor chen asked me to confirm my follow up for next friday at three fifteen",
            "i mean at eight forty five because the lab closes early that day",
            "Doctor Chen asked me to confirm my follow up for next Friday at 3:15. I mean, at 8:45, because the lab "
            "closes early that day.",
        ),
        (
            "मुख्य फेरों का सेटअप ताज लैंड्स एंड के ballroom में",
            "wait जेडब्ल्यू मैरियट जुहू के lotus lawn में रखना",
            "मुख्य फेरों का सेटअप ताज लैंड्स एंड के ballroom में—wait, जेडब्ल्यू मैरियट जुहू के lotus lawn में रखना।",
        ),
        (
            "ecg normal hai",
            "make that borderline t wave changes hai",
            "ECG normal hai. Make that borderline T wave changes hain.",
        ),
    ],
)
def test_round_b6_a_cue_opening_a_piece_kept_in_the_answer_is_refused(prior: str, text: str, wrote: str) -> None:
    polished = read_answer(text, wrote, prior=prior)
    assert polished.rejected == Reject.KEPT_CORRECTION


@pytest.mark.parametrize(
    ("prior", "text", "wrote"),
    [
        # v2-backtracking-en-00082: an apology opening a piece stays.
        (
            "I dropped off Aarav at the clinic.",
            "Sorry for the confusion at check in the nurse said to wait for the lab slip",
            "I dropped off Aarav at the clinic. Sorry for the confusion at check-in. The nurse said to wait for the "
            "lab slip.",
        ),
        (
            "we should finish the report",
            "wait for the numbers from finance",
            "We should finish the report. Wait for the numbers from finance.",
        ),
    ],
)
def test_round_b6_a_plain_cue_word_opening_a_piece_stays(prior: str, text: str, wrote: str) -> None:
    assert read_answer(text, wrote, prior=prior).rejected is None


def test_round_b6_a_word_said_on_both_sides_of_a_correction_may_be_written_once() -> None:
    # v2-backtracking-hi-00148: the fluent answer merged the two "स्लॉट" around the cue.
    said = (
        "राहुल को मैसेज भेजना कि उसके हेडफोन का डिलीवरी स्लॉट आज सुबह दस से बारह के बीच रहेगा नहीं नहीं कुरियर ने "
        "स्लॉट बदलकर कल शाम पांच से सात का कर दिया है"
    )
    wrote = "राहुल को मैसेज भेजना कि उसके हेडफोन का डिलीवरी स्लॉट कुरियर ने बदलकर कल शाम 5 से 7 का कर दिया है।"
    assert verdict(said, wrote, []) is None


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        # founder-003: three steps after "do one thing", joined by "then" across a full stop, make a list.
        (
            "Let's do one thing, call archana, then we'll talk to Arun. Then we can talk to Reliance team in the "
            "entire.",
            "Let's do one thing: call Archana, then we'll talk to Arun. Then we can talk to Reliance team in the "
            "entire.",
        ),
        (
            "ek kaam karo pehle bank jao phir passbook update karao phir mujhe balance bata do",
            "Ek kaam karo: pehle bank jao, phir passbook update karao, phir mujhe balance bata do.",
        ),
    ],
)
def test_round_b6_three_steps_after_do_one_thing_written_as_prose_are_refused(said: str, wrote: str) -> None:
    found = verdict(said, wrote, [])
    assert found is not None and found.rule == Reject.UNLISTED


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        ("do one thing call priya then send her the file", "Do one thing: call Priya, then send her the file."),
        (
            "एक काम करो डॉक्टर से बात करो फिर जो वो कहें वही करना फिर मुझे बताना",
            "एक काम करो: डॉक्टर से बात करो, फिर जो वो कहें वही करना, फिर मुझे बताना।",
        ),
    ],
)
def test_round_b6_two_steps_or_a_step_on_what_someone_says_stay_prose(said: str, wrote: str) -> None:
    assert verdict(said, wrote, []) is None


def test_round_b6_a_said_sentence_glued_into_a_list_item_is_refused() -> None:
    # founder-021 (round b6 A/B): "Keep my laptop ..." was run into item 2.
    said = (
        "Okay i need to do the following first turn off the tv second Turn off the lamps. Keep my laptop in a place "
        "where I cannot use it. But"
    )
    wrote = (
        "Okay, I need to do the following:\n1. Turn off the TV\n2. Turn off the lamps Keep my laptop in a place where "
        "I cannot use it."
    )
    found = verdict(said, wrote, [])
    assert found is not None and found.rule == Reject.DROPPED_ITEM


def test_round_b6_asking_someone_what_to_do_is_no_direct_question() -> None:
    # founder-006 (round b6 A/B): the recognizer's "?" kept on an indirect question; mended to a full stop.
    said = (
        "Ishaan do 1 thing. Get in touch with Aran directly for this and whatever he says then call archana. Then you "
        "can ask Pankaj John what to do and what not to do?"
    )
    wrote = (
        "Ishaan, do 1 thing. Get in touch with Aran directly for this, and whatever he says, then call Archana. Then "
        "you can ask Pankaj John what to do and what not to do?"
    )
    polished = read_answer(said, wrote)
    assert (polished.rejected, polished.text) == (None, wrote[:-1] + ".")
    assert verdict("can you ask him what to do", "Can you ask him what to do?", []) is None


@pytest.mark.parametrize(
    ("said", "wrote", "kept"),
    [
        # v2-punctuation-hinglish-00479, v2-backtracking-hinglish-00124, suite-growth-hinglish-06217: a Roman Hindi
        # word respelled goes back to the speaker's spelling.
        ("usne pucha ki meeting kab hai", "Usne poocha ki meeting kab hai.", "Usne pucha ki meeting kab hai."),
        ("aaj dupahar teen baje aana", "Aaj dopahar 3 baje aana.", "Aaj dupahar 3 baje aana."),
        ("apni target sheet lani hai", "Apni target sheet laani hai.", "Apni target sheet lani hai."),
    ],
)
def test_round_b6_a_roman_hindi_word_respelled_goes_back_to_the_said_spelling(said: str, wrote: str, kept: str) -> None:
    assert judge("", said, [], None, wrote).text == kept
