package com.thesis.benchmark.nexmark;

import java.io.Serializable;

/** Nexmark Auction event (canonical schema). */
public class Auction implements Serializable {
    public long id;
    public String itemName;
    public long initialBid;
    public long reserve;
    public long dateTime;     // event-time millis (creation time)
    public long expires;      // millis at which auction closes
    public long seller;       // person id
    public long category;

    public Auction() {}

    public Auction(long id, String itemName, long initialBid, long reserve,
                   long dateTime, long expires, long seller, long category) {
        this.id = id;
        this.itemName = itemName;
        this.initialBid = initialBid;
        this.reserve = reserve;
        this.dateTime = dateTime;
        this.expires = expires;
        this.seller = seller;
        this.category = category;
    }
}
