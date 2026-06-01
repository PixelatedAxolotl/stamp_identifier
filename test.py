import requests

try:

    link = input("Image-link: ")
    response = requests.get(f"{link}", timeout=5)

    if response.status_code == 200:
        #print(link)

        print(f"Google: https://lens.google.com/uploadbyurl?url={link}\n"
              f"Bing: https://www.bing.com/images/search?view=detailv2&iss=sbi&form=SBIVSP&sbisrc=UrlPaste&q=imgurl:{link}\n"
              f"Yandex: https://yandex.com/images/search?source=collections&&url={link}&rpt=imageview\n"
              f"Baidu: https://graph.baidu.com/details?isfromtusoupc=1&tn=pc&carousel=0&image={link}")

    else:

        print("Invalid link.")


except Exception as e:
    print(e)