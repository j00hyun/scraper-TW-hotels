# -*- coding: utf-8 -*-
import re
import random
import time
import logging
from typing import List, Dict, MutableMapping, Optional
import requests
from requests import Session, Response
from requests.cookies import RequestsCookieJar
from lxml import etree
from retry import retry
from .excepts import ReqSysAbnoramlError
from bs4 import BeautifulSoup
from fake_useragent import UserAgent
from selenium import webdriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.ui import Select
from store.excel import ExcelStore
from .dto import CountyOption, TotalPageOfCounty, HotelInfo, HotelField


class TaiwanHotelParserAgent(object):
    PARSED_COLUMNS = [
        HotelField.Name,
        HotelField.Address,
        HotelField.Phone,
        HotelField.Email,
        HotelField.Rooms,
        HotelField.Prices,
        HotelField.Url
    ]
    WEBSITE_URL = "https://taiwanstay.net.tw"
    SEARCH_ROUNTE = "/tourism_web/search.php"
    HOTEL_PAGE_ROUTE = "/tourism_web/hotel_content.php"
    ABNORMAL_ROUTE = "/system_abnormal.php"
    SEARCH_URL = WEBSITE_URL + SEARCH_ROUNTE
    HOTEL_PAGE_URL = WEBSITE_URL + HOTEL_PAGE_ROUTE
    ABNORMAL_URL = WEBSITE_URL + ABNORMAL_ROUTE
    CITIES_CODE = {
        "F": "新北市", "A": "臺北市", "H": "桃園市", "B": "臺中市", "R": "臺南市",
        "S": "高雄市", "G": "宜蘭縣", "J": "新竹縣", "K": "苗栗縣", "N": "彰化縣",
        "M": "南投縣", "P": "雲林縣", "Q": "嘉義縣", "T": "屏東縣", "V": "臺東縣",
        "U": "花蓮縣", "X": "澎湖縣", "C": "基隆市", "O": "新竹市", "I": "嘉義市",
        "W": "金門縣", "Z": "連江縣"
    }

    def __init__(self, selected_code: str, excelstore: ExcelStore) -> None:
        self._selected_code: str = selected_code
        self._payload: dict = {
            "page": 1,
            "sortBy": None,
            "act": None,
            "sel_hotel[]": [1, 2, 3],
            "sel_keyword": None,
            "sel_city": None,
            "sel_keyword": None,
            "sel_city": None,
            "sel_area": None,
            "sel_price": None,
            "sel_room_num": None,
            "sel_type": None,
        }
        self._excelstore = excelstore

    def _get_selected_city_counties(self, city_name: str) -> List[CountyOption]:
        """
        藉由 Selenium 模擬點擊縣市，取得指定縣市的所有省區資料
        Returns:
            List[CountyOption]: 此城市的所有省區
        """
        options = webdriver.ChromeOptions()
        # 不開啟 Browser 的 GUI
        options.headless = True
        driver = webdriver.Chrome(chrome_options=options)
        driver.get(self.WEBSITE_URL)
        selector: Select = Select(driver.find_element_by_xpath("//*[@id='sel_city']"))
        selector.select_by_value(city_name)
        counties_options = driver.find_elements_by_xpath("//*[@id='sel_area']/option")
        counties: List[CountyOption] = [
            CountyOption(option.text, option.get_attribute("value"))
            for option in counties_options if option.get_attribute("value")
        ]
        print(f"完成網頁模擬，選擇縣市: {city_name}，取得「{city_name}」所有的市區鄉鎮")
        return counties

    def _gen_fake_header(self) -> dict:
        """
        產生訪問網頁用的假 Header
        Returns:
            [dict]: 產生好的假 Header 標頭
        """
        fake_ua = UserAgent()
        random_ua = random.choice([fake_ua.chrome,
                                   fake_ua.opera,
                                   fake_ua.ie,
                                   fake_ua.firefox,
                                   fake_ua.safari,
                                   fake_ua.google,
                                   fake_ua.ff,
                                   fake_ua.random])
        header = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Host": "taiwanstay.net.tw",
            "Pragma": "no-cache",
            "Upgrade-Insecure-Requests": "1",
            "User-Agent": random_ua
        }
        print("產生的 Fake Header: {}".format(header["User-Agent"]))
        return header

    def _build_req_session(self, headers: dict, cookies: RequestsCookieJar):
        """
        建立請求用的 Session 以保持在不同的請求保持相同的紀錄
        Args:
            headers (dict): 要建立 Request Session 用的 Headers
            cookies (dict): 要建立 Request Session 用的 Cookies
        Returns:
            Session: 回傳 Requests 建立的 Session
        """
        req_session = Session()
        req_session.headers = headers
        req_session.cookies = cookies
        return req_session

    def _delay_continue(self, min: float, max: float) -> float:
        """
        延遲並繼續，為了並免請求的次數之間時間過近，透過給予時間的範圍，隨機生成延遲的時間並於完成等待後繼續執行
        Args:
            min (float): 隨機時間最小範圍，單位為秒
            max (float): 隨機時間最大範圍，單位為秒
        Returns:
            float: 回傳生成的等待時間
        """
        # 隨機產生在 1 - 2 的福點數時間範圍，並格式化成小數點兩位
        rand_sec = float("{:.2f}".format(random.uniform(1.5, 2.2)))
        time.sleep(rand_sec)
        return rand_sec

    def _check_does_normal_resp(self, resp: Response) -> bool:
        if resp.url == self.ABNORMAL_URL:
            lxmltree = etree.HTML(resp.content)
            content = etree.tostring(lxmltree, method='html', pretty_print=True).decode('utf-8')
            raise ReqSysAbnoramlError(resp.status_code, "解析旅館資料異常！皆為 None", resp.url, content)
        return True

    @retry(exceptions=ReqSysAbnoramlError, tries=3, delay=5)
    def retryable_requests(self,
                           url: str,
                           payload: dict,
                           headers: Optional[dict] = None,
                           cookies: Optional[RequestsCookieJar] = None) -> Response:
        try:
            resp = requests.get(url, params=payload, headers=headers, cookies=cookies)
            print(f"Response Cookies: {resp.cookies}")
            self._check_does_normal_resp(resp)
            return resp
        except ReqSysAbnoramlError as rse:
            print(f" ！ 網站異常 ！ #########################################")
            print(f">> 請求網址: {url}, payload: {payload}, headers: {headers}, cookies: {cookies}")
            print(f">> 回應網址：{rse.url}, 頁面狀態碼： {rse.http_code}\n" + rse.content)
            raise rse

    def _get_total_page_of_county(self, city: str, county: CountyOption) -> TotalPageOfCounty:
        """
        取得指定的縣市與下的特定市區鄉鎮的旅館頁面總頁數
        Args:
            city (str): 縣市名
            county (CountyOption): 市區鄉鎮 DTO
        Returns:
            TotalPageOfCounty: 市區鄉鎮的總頁數 DTO
        """
        try:
            self._payload["sel_city"] = city
            self._payload["sel_area"] = county.value
            resp = self.retryable_requests(self.SEARCH_URL,
                                           self._payload,
                                           headers=self._gen_fake_header())
            soup = BeautifulSoup(resp.content, "html.parser")
            page_with_num = soup.find("span", class_="totalbox")
            pages = page_with_num.find_all("span")[0].text
            numbers = page_with_num.find_all("span")[1].text
            return TotalPageOfCounty(int(pages), (numbers))
        except Exception as e:
            print(f"取得所有頁數時異常！")
            raise e

    def _get_hotels_of_pages(self, pages: int) -> List[HotelInfo]:
        """
        取得該市區鄉鎮的所有頁面下總旅館資料
        Args:
            pages (int): 該市區鄉鎮的總頁數
        Returns:
            List[HotelInfo]: 該市區鄉鎮的所有旅館資料
        """
        try:
            hotels_class_css = "col-md-12 col-sm-12 nopadding items-container simple-items"
            hotels_of_pages = []
            for page in range(1, pages + 1):
                print(f"#### 開始第 {page} 頁 ################################")
                self._payload["page"] = page
                resp = self.retryable_requests(self.SEARCH_URL,
                                               self._payload,
                                               headers=self._gen_fake_header())
                hotels_id: List[int] = self._get_hotels_id_of_current_page(page, resp.content)
                hotels = self._retrieve_hotels_of_current_page(hotels_id)
                hotels_of_pages.extend(hotels)
            return hotels_of_pages
        except Exception as e:
            print(f"取得 {page} 頁的所有 Hotels 時異常 ！")
            raise e

    def _get_hotels_id_of_current_page(self, page: int, html: bytes) -> List[int]:
        """
        取得目前此頁面下顯示的所有旅館 id，作為訪問用
        Args:
            html (bytes): 該頁面的 HTML 內容
        Returns:
            List[int]: 該頁面的所有旅館 id
        """
        try:
            # 透過 /@href 語法直接取得連結屬性
            hotel_links_xpath = "//*[@id='searchpage']/div/div/div[3]/div/div/a/@href"
            hotelstree = etree.HTML(html)
            hotel_links = hotelstree.xpath(hotel_links_xpath)
            hotels_id = [link.split("hotel_id=")[1] for link in hotel_links]
            return hotels_id
        except Exception as e:
            print(f"解析第 {page} 頁的所有旅館訪問連結取得 id 時異常 ！")
            raise e

    def _retrieve_hotels_of_current_page(self, hotels_id: List[int]) -> List[HotelInfo]:
        """
        透過該頁面下的所有旅館 id 取得旅館資料
        Args:
            hotels_id (List[int]): 該頁面的所有旅館 id

        Returns:
            List[HotelInfo]: 該頁面的所有旅館資料
        """
        hotels = []
        for index, hotel_id in enumerate(hotels_id):
            # 隨機產生在 1.5 - 2.2 之間的延遲
            delay = self._delay_continue(1.5, 2.2)
            print(f"開始爬取旅館 => 索引：{index}, ID: {hotel_id}, 隨機延遲時間為: {delay} secs")
            hotel = self._retrieve_hotel_info_by_id(hotel_id)
            hotels.append(hotel)
        return hotels

    def _retrieve_hotel_info_by_id(self, hotel_id: int) -> HotelInfo:
        """
        透過訪問旅館資料的 id 取得該旅館頁面下的旅館相關資訊
        Args:
            hotel_id (int): 旅館 id
        Returns:
            HotelInfo: 該旅館資料
        """
        try:
            payload = {"hotel_id": hotel_id}
            resp = self.retryable_requests(self.HOTEL_PAGE_URL,
                                           payload,
                                           headers=self._gen_fake_header())
            parsed = {}
            parsing_xpath = {
                HotelField.Name: "//*[@id='right-hotel']/h2/text()",
                HotelField.Address: "//*[@id='right-hotel']/div[4]/div[2]/p/span[2]/text()",
                HotelField.Phone: "//*[@id='tel_div']/p/span[2]/text()",
                HotelField.Email: "//*[@id='email_div']/a/p/span[2]/text()",
                HotelField.Rooms: "//*[@id='right-hotel']/div[5]/div[2]/p/span[2]/text()",
                HotelField.Prices: "//*[@id='right-hotel']/div[5]/div[3]/p/span[2]/text()",
                HotelField.Url: "//*[@id='website_div']/p/span[2]/a"
            }
            retreived_func = {
                HotelField.Name: lambda elems: elems[0] if elems else None,
                HotelField.Address: lambda elems: elems[0] if elems else None,
                HotelField.Phone: lambda elems: elems[0] if elems else None,
                HotelField.Email: lambda elems: elems[0] if elems else None,
                HotelField.Rooms: lambda elems: elems[0] if elems else None,
                HotelField.Prices: lambda elems: elems[0] if elems else None,
                HotelField.Url: lambda elems: elems[0].get("href") if elems and elems[0].get("href") else None
            }
            hoteltree = etree.HTML(resp.content)
            for field, xpath in parsing_xpath.items():
                parsed[field] = retreived_func[field](hoteltree.xpath(xpath))

            hotel = {
                HotelField.Id: str(hotel_id),
                HotelField.Name: parsed[HotelField.Name],
                HotelField.Phone: parsed[HotelField.Phone],
                HotelField.Address: parsed[HotelField.Address],
                HotelField.Rooms: parsed[HotelField.Rooms],
                HotelField.Prices: parsed[HotelField.Phone],
                HotelField.Email: parsed[HotelField.Email],
                HotelField.Url: parsed[HotelField.Url],
            }
            print(f"------------- 完成爬取，旅館資料 -------------------------")
            print(f" - id: {hotel_id}")
            print(f" - 名稱: {parsed[HotelField.Name]}")
            print(f" - 訂房電話： {parsed[HotelField.Phone]}")
            print(f" - 地址: {parsed[HotelField.Address]}")
            print(f" - 總房間數: {parsed[HotelField.Rooms]}")
            print(f" - 定價: {parsed[HotelField.Phone]}")
            print(f" - 連絡信箱: {parsed[HotelField.Email]}")
            print(f" - 網站連結: {parsed[HotelField.Url]} \n")
            return hotel
        except Exception as e:
            print(f"解析旅館 {hotel_id} 的資訊頁面異常！")
            raise e

    def _store_excel(self, county_name: str, hotels: List[HotelInfo]):
        try:
            # 新增此市區鄉鎮的 Sheet
            sheet = self._excelstore.add_sheet(county_name, self.PARSED_COLUMNS)
            # 抓出每一的鄉鎮的所有頁面資料
            for idx, hotel in enumerate(hotels):
                # 第 0 列為 Header
                row = idx + 1
                self._excelstore.store_hotel(sheet, row, self.PARSED_COLUMNS, hotel)
        except Exception as e:
            print(" ！ 寫入 Excel 異常 ！ ")
            raise e

    def start_parsing(self) -> ExcelStore:
        # 先對每一個城市爬蟲個鄉鎮
        try:
            city = self.CITIES_CODE[self._selected_code]
            counties: List[CountyOption] = self._get_selected_city_counties(city)
            for county in counties:
                total: TotalPageOfCounty = self._get_total_page_of_county(city, county)
                print(f"==== 開始抓取城市: {city} {county.name}, 共有 {total.pages} 頁，{total.num_of_hotels} 筆 ====")
                hotels_of_county: List[HotelInfo] = self._get_hotels_of_pages(total.pages)
                print(f"寫入 {county.name} 資料至 Excel ....")
                self._store_excel(county.name, hotels_of_county)
                print(f"#### 完成爬取 {county.name} 的旅館資料 ... !")
            return self._excelstore
        except Exception as e:
            raise e
        finally:
            self._excelstore.close()
