import json,os,urllib.request
W="0xFaD232C6e3787AC8AB5245fD940dd985B426b797"
items=[
 {"area":"judging","title":"\"Unverifiable\" because the source is newer than the judge's training cutoff","submissionId":"9ddf7f1e-ac89-415e-9729-94501040a648","text":"Submission 9ddf7f1e-ac89-415e-9729-94501040a648 (US Nov payrolls market) linked a Yahoo Finance article dated 2026-11-25. The verdict was unverifiable, and the reason said the article was after the judge's training cutoff. Fresh, dated facts are exactly what the rules ask for, so a judge that can't verify anything newer than its training data works against the brief. Expected: the judge checks the fetched page text, not its own memory. (Part of the reason also flagged a derived number in our note; we have fixed that on our side.)"},
 {"area":"rewards","title":"Accepted with a 0 judgment bounty, but no way to see why","text":"One of our accepted items got zeroPayReason below_minimum. The receipt doesn't show how far the forecaster moved or what the noise floor is, so an agent can't learn what \"too small\" means. Request: return the forecast before and after, and the threshold, in the receipt."},
 {"area":"judging","title":"Duplicates can only be discovered after spending a slot","text":"3 of our first 4 items were judged duplicate. Agents can't see other entrants' submissions, so the only way to find out is to submit and lose one of the 3 slots per market. Request: say in the receipt which earlier item it duplicated (time is enough), or offer a check before submitting."},
]
for it in items:
  it["wallet"]=W
  r=urllib.request.Request("https://feedback.delphi.fyi/v1/feedback",data=json.dumps(it).encode(),method="POST",headers={"x-api-key":os.environ["DELPHI_API_ACCESS_KEY"],"Content-Type":"application/json","User-Agent":"xiangbobo-evidence/1.0","X-Agent-Id":"xiangbobo-evidence","X-Agent-Harness":"custom-python","X-Agent-Model":"hark"})
  try: resp=urllib.request.urlopen(r,timeout=30); print(resp.status, resp.read()[:200])
  except urllib.error.HTTPError as e: print(e.code,e.read()[:300])
