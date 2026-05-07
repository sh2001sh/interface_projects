#include "codec.h"
#include <iostream>                 
#include <QByteArray>                 
#include <QDataStream>                 
#include <algorithm>                 
#include <string>                 
#include <QDebug>                
#include <QMap>                
#include <functional>              
static int BigAndLittleFlag = 1;
#define BigtoLittle32(A) ((((uint32_t)(A) & 0xff000000) >> 24) | (((uint32_t)(A) & 0x00ff0000) >> 8) | (((uint32_t)(A) & 0x0000ff00) << 8) | (((uint32_t)(A) & 0x000000ff) << 24))
#define LittleToBig32(A) ((((uint32_t)(A) & 0xff000000) >> 24) | (((uint32_t)(A) & 0x00ff0000) >> 8) | (((uint32_t)(A) & 0x0000ff00) << 8) | (((uint32_t)(A) & 0x000000ff) << 24))             
class MessCodeInfo{                
public:                 
	int   icycle;                        
	int   itimes;                         
	QString strSeq;                    
};         
static quint64 readBits(const uchar * data, int dataLength, int startBitIndex, int numBits)
{
        
	if (numBits <= 0 || numBits > 64 || startBitIndex < 0 || startBitIndex + numBits >(dataLength * 8)) {            
		qWarning() << "Invalid parameters";            
		return 0;        
	}        
	quint32 result = 0;        
	// 计算起始字节和起始位在该字节的位置        
	int startByteIndex = startBitIndex / 8;        
	int startBitInStartByte = startBitIndex % 8;        
	// 遍历所有影响结果的字节        
	for (int i = startByteIndex; numBits > 0 && i < dataLength; ++i) {           
 		quint8 currentByte = data[i];           
 		// 计算本次需要读取的位数（不超过8位）           
 		int bitsToRead = std::min(numBits, 8 - startBitInStartByte);           
 		// 移动当前字节中的目标位到最低位，然后按位与以保留目标位           
 		quint8 shiftedByte = (currentByte >> (8 - startBitInStartByte - bitsToRead)) & ((1u << bitsToRead) - 1);            
		// 左移结果并将新读取的位添加进去            
		result = (result << bitsToRead) | shiftedByte;           
 		// 更新剩余位数和下次循环的起始位位置           
 		numBits -= bitsToRead;            
		startBitInStartByte = 0;        
	}        
	return result;    
}
        
static void checkPosition(uchar * &pData,int & msgLens,int & pos,int num )
{    
	if((pos + num ) > 32)
	{        
		pData += pos / 8;        
		msgLens -= pos / 8;        
		pos = pos % 8;    
	}
}
static quint64 readBitsLE(const uchar * data, int dataLength, int startBitIndex, int numBits)
{
    if (numBits <= 0 || numBits > 64 || startBitIndex < 0 || startBitIndex + numBits >(dataLength * 8)) {
        qWarning() << "Invalid parameters";
        return 0;
    }
    quint64 result = 0;
    char* p = (char*)&BigAndLittleFlag;
    if (*p == 1)//小端
    {
        int startByteIndex = startBitIndex / 8;                                                      
        int startBitInStartByte = startBitIndex % 8;                                                 
        int readBits = numBits % 8;                                                                  
        if (startBitInStartByte == 0 && readBits == 0)//整字节                                       
        {                                                                                            
            memcpy(&result, data + startByteIndex, numBits / 8);                                     
        }                                                                                            
        else//按为读取                                                                               
        {                                                                                            
            // 遍历所有影响结果的字节                                                                
            for (int i = startByteIndex; numBits > 0 && i < dataLength; ++i) {                       
                quint8 currentByte = data[i];                                                        
                // 计算本次需要读取的位数（不超过8位）                                               
                int bitsToRead = std::min(numBits, 8 - startBitInStartByte);                         
                // 移动当前字节中的目标位到最低位，然后按位与以保留目标位                            
                quint8 shiftedByte = (currentByte >> startBitInStartByte) & ((1u << bitsToRead) - 1);
                // 左移结果并将新读取的位添加进去                                                    
                result |= shiftedByte << ((i - startByteIndex) * bitsToRead);                        
                // 更新剩余位数和下次循环的起始位位置                                                
                numBits -= bitsToRead;                                                               
                startBitInStartByte = 0;                                                             
            }                                                                                        
        }                                                                                            
    }                                                                                                
    else//大端                                                                                       
    {                                                                                                
        // 计算起始字节和起始位在该字节的位置                                                        
        int startByteIndex = startBitIndex / 8;                                                      
        int startBitInStartByte = startBitIndex % 8;                                                 
        // 遍历所有影响结果的字节                                                                    
        for (int i = startByteIndex; numBits > 0 && i < dataLength; ++i) {                           
            quint8 currentByte = data[i];                                                            
            // 计算本次需要读取的位数（不超过8位）                                                   
            int bitsToRead = std::min(numBits, 8 - startBitInStartByte);                             
            // 移动当前字节中的目标位到最低位，然后按位与以保留目标位                                
            quint8 shiftedByte = (currentByte >> startBitInStartByte) & ((1u << bitsToRead) - 1);    
            // 左移结果并将新读取的位添加进去                                                        
            result |= shiftedByte << (numBits - bitsToRead);                                         
            // 更新剩余位数和下次循环的起始位位置                                                    
            numBits -= bitsToRead;                                                                   
            startBitInStartByte = 0;                                                                 
        }                                                                                            
    }                                                                                                
    return result;                                                                                   
}                                                                                                    
static void appendBits(uint64_t value, size_t bitSize, QByteArray & byteArray, bool bigEndian ,bool bclear = false)       
{                                                                                                    
    static uint64_t cachedBits = 0; // 缓存的位数据                                                  
    static size_t cachedBitCount = 0; // 已缓存的位数                                                
    char* p = (char*)&BigAndLittleFlag;                                                              
	if(bclear)
	{                    
		cachedBits = 0;                    
		cachedBitCount = 0;                
	}
    if (*p == 1)//小端                                                                               
    {                                                                                                
        if (bigEndian)                                                                               
        {                                                                                            
            if (bitSize > 8)                                                                         
            {                                                                                        
              value &=(1<<bitSize) -1;                                                       
            }                                                                                        
            // 将新数据加入缓存                                                                      
            cachedBits = (cachedBits << bitSize) | (value & ((1 << bitSize) - 1));                   
            cachedBitCount += bitSize;                                                               
                                                                                                     
        }                                                                                            
        else                                                                                         
        {                                                                                            
            // 将新数据加入缓存                                                                      
            //cachedBits = (cachedBits << bitSize) | (value & ((1 << (bitSize)) - 1));               
            if (cachedBitCount == 0 && (bitSize % 8 == 0))
           {
               cachedBits = value;
            }
            else
            cachedBits = (cachedBits) | (value & ((1 << (bitSize + cachedBitCount)) - 1));           
            cachedBitCount += bitSize;                                                               
        }                                                                                            
        while (cachedBitCount >= 8)                                                                  
        {                                                                                            
            // 计算当前字节的位移                                                                    
            size_t shift = bigEndian ? cachedBitCount - 8 : 0;                                     
            // 获取当前字节                                                                          
            char byte = static_cast<char>((cachedBits >> shift) & 0xFF);                           
            //char byte = static_cast<char>(cachedBits & 0xFF);                                        
            // 添加到字节数组                                                                        
            byteArray.append(byte);                                                                  
            // 更新缓存中的位数                                                                      
            cachedBitCount -= 8;                                                                     
            //// 如果是大端，左移；如果是小端，右移                                                  
            if (bigEndian)                                                                         
              cachedBits &= (1<< cachedBitCount) -1;                                                                        
            else                                                                                   
              cachedBits >>= 8;
        }                                                                                            
    }                                                                                                
    else//大端                                                                                       
    {                                                                                                
    }                                                                                                
}     
static double readBits_d(const uchar* data, int dataLength, int startBitIndex, int numBits)    
{        
	if (numBits <= 0 || numBits > 64 || startBitIndex < 0 || startBitIndex + numBits >(dataLength * 8)) {            
		qWarning() << "Invalid parameters";            
		return 0.0;        
	}        
	double result = 0.0;        
	int startByteIndex = startBitIndex / 8;        
	int startBitInStartByte = startBitIndex % 8;        
	int bitsRemaining = numBits;        
	for (int i = startByteIndex; bitsRemaining > 0 && i < dataLength; ++i) {            
		quint8 currentByte = data[i];            
		int bitsToRead = std::min(bitsRemaining, 8 - startBitInStartByte);            
		quint8 shiftedByte = (currentByte >> (8 - startBitInStartByte - bitsToRead)) & ((1u << bitsToRead) - 1);            
		result += static_cast<double>(shiftedByte) * pow(2, -bitsRemaining);            
		bitsRemaining -= bitsToRead;            
		startBitInStartByte = 0;        
	}        
	return result;   
	 }    
static double readBitsLE_d(const uchar * data, int dataLength, int startBitIndex, int numBits)    
{        
	if (numBits <= 0 || numBits > 64 || startBitIndex < 0 || startBitIndex + numBits >(dataLength * 8)) {            
		qWarning() << "Invalid parameters";            
		return 0.0;        
	}        
	double result = 0.0;        
	int startByteIndex = startBitIndex / 8;        
	int startBitInStartByte = startBitIndex % 8;        
	int bitsRemaining = numBits;        
	for (int i = startByteIndex; bitsRemaining > 0 && i < dataLength; ++i) {            
		quint8 currentByte = data[i];            
		int bitsToRead = std::min(bitsRemaining, 8 - startBitInStartByte);            
		quint8 shiftedByte = (currentByte >> startBitInStartByte) & ((1u << bitsToRead) - 1);            
		result += static_cast<double>(shiftedByte) * pow(2, -(startBitIndex + numBits - (i * 8 + startBitInStartByte)));            
		bitsRemaining -= bitsToRead;            
		startBitInStartByte = 0;        
	}        
	return result;    
}    
static void appendBits_d(double value, size_t bitSize, QByteArray& byteArray, bool bigEndian)    
{                
	static qulonglong cachedBits = 0; // Cached bit data                
	static size_t cachedBitCount = 0; // Number of bits already cached                
	// Convert double to 64-bit integer                
	uint64_t intValue = *reinterpret_cast<qulonglong*>(&value);                
	// Mask value to only keep the desired number of bits                
	intValue &= ((1ULL << bitSize) - 1);                
	// Add new data to the cache                
	cachedBits = (cachedBits << bitSize) | intValue;                
	cachedBitCount += bitSize;                
	while (cachedBitCount >= 8)                
	{                    
		// Calculate the bit shift for the current byte                    
		size_t shift = bigEndian ? cachedBitCount - 8 : 0;                    
		// Get the current byte                    
		char byte = static_cast<char>((cachedBits >> shift) & 0xFF);                    
		// Append the byte to the byte array                    
		byteArray.append(byte);                    
		// Update the number of cached bits                    
		cachedBitCount -= 8;                    
		// Adjust the cached bits based on endianness                    
		if (bigEndian)                    
		    cachedBits &= ((1ULL << shift) - 1);                    
		else                    
		    cachedBits >>= 8;                
	}            
	}    

void readOrigin(W304&w304,uchar * & pMsgData, int & msgLens,int & pos){
// body
	checkPosition(pMsgData, msgLens,pos,3);
	w304.var17446767732 = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	checkPosition(pMsgData, msgLens,pos,15);
	w304.var17446773733 = readBits(pMsgData, msgLens, pos ,15);
	pos += 15;

	checkPosition(pMsgData, msgLens,pos,8);
	w304.var17446774734 = readBits(pMsgData, msgLens, pos ,8);
	pos += 8;

	checkPosition(pMsgData, msgLens,pos,3);
	w304.var17446785735 = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	checkPosition(pMsgData, msgLens,pos,5);
	w304.var17446798736 = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

	checkPosition(pMsgData, msgLens,pos,6);
	w304.var17446807737 = readBits(pMsgData, msgLens, pos ,6);
	pos += 6;

	checkPosition(pMsgData, msgLens,pos,6);
	w304.var17446815738 = readBits(pMsgData, msgLens, pos ,6);
	pos += 6;

	checkPosition(pMsgData, msgLens,pos,14);
	w304.var17446818739 = readBits(pMsgData, msgLens, pos ,14);
	pos += 14;

}

void readProlong(W304&w304,uchar * & pMsgData, int & msgLens,int & pos){
// head

	checkPosition(pMsgData, msgLens,pos,2);
	w304.var1744769771 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	w304.var1744779772 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

// body
	checkPosition(pMsgData, msgLens,pos,1);
	w304.var17446832740 = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,16);
	w304.var17446840741 = readBits(pMsgData, msgLens, pos ,16);
	pos += 16;

	checkPosition(pMsgData, msgLens,pos,26);
	w304.var17446848742 = readBits(pMsgData, msgLens, pos ,26);
	pos += 26;

	checkPosition(pMsgData, msgLens,pos,25);
	w304.var17446848743 = readBits(pMsgData, msgLens, pos ,25);
	pos += 25;

}

void readContinue1(W304&w304,uchar * & pMsgData, int & msgLens,int & pos){
// head

	checkPosition(pMsgData, msgLens,pos,2);
	w304.var1744784773 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	w304.var1744796776 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,5);
	w304.var17447117779 = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

// body
//位数为0的字段，用头中的**字段值代替
	w304.var17446920751 = w304.var1744784773;
	checkPosition(pMsgData, msgLens,pos,14);
	w304.var17446864744 = readBits(pMsgData, msgLens, pos ,14);
	pos += 14;

	checkPosition(pMsgData, msgLens,pos,14);
	w304.var17446864745 = readBits(pMsgData, msgLens, pos ,14);
	pos += 14;

	checkPosition(pMsgData, msgLens,pos,14);
	w304.var17446880746 = readBits(pMsgData, msgLens, pos ,14);
	pos += 14;

	checkPosition(pMsgData, msgLens,pos,5);
	w304.var17446880747 = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

	checkPosition(pMsgData, msgLens,pos,5);
	w304.var17446897748 = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

	checkPosition(pMsgData, msgLens,pos,4);
	w304.var17446903749 = readBits(pMsgData, msgLens, pos ,4);
	pos += 4;

	checkPosition(pMsgData, msgLens,pos,3);
	w304.var17446912750 = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	checkPosition(pMsgData, msgLens,pos,4);
	w304.var17446920751 = readBits(pMsgData, msgLens, pos ,4);
	pos += 4;

}

void readContinue2(W304&w304,uchar * & pMsgData, int & msgLens,int & pos){
// head

	checkPosition(pMsgData, msgLens,pos,2);
	w304.var1744784774 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	w304.var1744796777 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,5);
	w304.var17447117780 = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

// body
	checkPosition(pMsgData, msgLens,pos,18);
	w304.var17446932752 = readBits(pMsgData, msgLens, pos ,18);
	pos += 18;

	checkPosition(pMsgData, msgLens,pos,18);
	w304.var17446940753 = readBits(pMsgData, msgLens, pos ,18);
	pos += 18;

	checkPosition(pMsgData, msgLens,pos,18);
	w304.var17446944754 = readBits(pMsgData, msgLens, pos ,18);
	pos += 18;

	checkPosition(pMsgData, msgLens,pos,3);
	w304.var17446952755 = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	checkPosition(pMsgData, msgLens,pos,3);
	w304.var17446960756 = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	checkPosition(pMsgData, msgLens,pos,3);
	w304.var17446972757 = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

}

void readContinue4(W304&w304,uchar * & pMsgData, int & msgLens,int & pos){
// head

	checkPosition(pMsgData, msgLens,pos,2);
	w304.var1744784775 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	w304.var1744796778 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,5);
	w304.var17447117781 = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

// body
//位数为0的字段，用头中的**字段值代替
	w304.var1744743766 = w304.var1744784775;
	checkPosition(pMsgData, msgLens,pos,16);
	w304.var17446978758 = readBits(pMsgData, msgLens, pos ,16);
	pos += 16;

	checkPosition(pMsgData, msgLens,pos,10);
	w304.var17446988759 = readBits(pMsgData, msgLens, pos ,10);
	pos += 10;

	checkPosition(pMsgData, msgLens,pos,1);
	w304.var17446993760 = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,1);
	w304.var174472761 = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,2);
	w304.var1744710762 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,15);
	w304.var1744718763 = readBits(pMsgData, msgLens, pos ,15);
	pos += 15;

	checkPosition(pMsgData, msgLens,pos,2);
	w304.var1744727764 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	w304.var1744734765 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,18);
	w304.var1744743766 = readBits(pMsgData, msgLens, pos ,18);
	pos += 18;

}
QString  checkW304SeqNum(QString strSeq)
{
	
	static QMap<QString,std::shared_ptr<MessCodeInfo>>  proSeqMap; 
	static int index = 0;                     
	if( 0 == index++ )
	{
     
	std::shared_ptr<MessCodeInfo> Seq_1(new MessCodeInfo); 
	Seq_1->icycle=0;
	Seq_1->itimes=1;
	Seq_1->strSeq="311212224";
	proSeqMap["Seq_1"] = Seq_1; 

	std::shared_ptr<MessCodeInfo> Seq_2(new MessCodeInfo); 
	Seq_2->icycle=2000;
	Seq_2->itimes=3;
	Seq_2->strSeq="3112122";
	proSeqMap["Seq_2"] = Seq_2; 

	std::shared_ptr<MessCodeInfo> Seq_3(new MessCodeInfo); 
	Seq_3->icycle=2000;
	Seq_3->itimes=3;
	Seq_3->strSeq="3112124";
	proSeqMap["Seq_3"] = Seq_3; 

	}
	QString ret;
	auto ptr = proSeqMap.begin();        
	while (ptr != proSeqMap.end())        
	{            
		if (ptr.value()->strSeq == strSeq)            
		{                
			ret = ptr.key();                
			break;            
		}            
		ptr++;        
	}

	return ret;
        
}
void readOrigin(W304&w304,uchar * & pMsgData, int & msgLens,int & pos);
void readProlong(W304&w304,uchar * & pMsgData, int & msgLens,int & pos);
void readContinue1(W304&w304,uchar * & pMsgData, int & msgLens,int & pos);
void readContinue2(W304&w304,uchar * & pMsgData, int & msgLens,int & pos);
void readContinue4(W304&w304,uchar * & pMsgData, int & msgLens,int & pos);
void readW304(int key,W304 &w304,uchar * & pMsgData, int & msgLens,int & pos)
{
	switch (key)
{
	case 3:readOrigin(w304,pMsgData,msgLens,pos);break; 
	case 11:readProlong(w304,pMsgData,msgLens,pos);break;
	case 21:readContinue1(w304,pMsgData,msgLens,pos);break;
	case 22:readContinue2(w304,pMsgData,msgLens,pos);break;
	case 24:readContinue4(w304,pMsgData,msgLens,pos);break;
	default:
		break;
	}
}

void writeContinue2(W304&w304,QByteArray& data);
void VerifyField(W304 &w304);
void updateGroupFlag(W304 &w304);

void writeContinue1(W304&w304,QByteArray& data);
void updateFieldValue(W304 &w304);

void writeProlong(W304&w304,QByteArray& data);

void writeOrigin(W304&w304,QByteArray& data);

void writeContinue4(W304&w304,QByteArray& data);
static void writeSeq_1(W304&w304,QByteArray& data)
{

	VerifyField(w304);
	updateFieldValue(w304);
	updateGroupFlag(w304);
	writeOrigin(w304,data);
	writeProlong(w304,data);
	writeContinue1(w304,data);
	writeContinue2(w304,data);
	writeContinue4(w304,data);
}
static void writeSeq_2(W304&w304,QByteArray& data)
{

	VerifyField(w304);
	updateFieldValue(w304);
	updateGroupFlag(w304);
	writeOrigin(w304,data);
	writeProlong(w304,data);
	writeContinue1(w304,data);
	writeContinue2(w304,data);
}
static void writeSeq_3(W304&w304,QByteArray& data)
{

	VerifyField(w304);
	updateFieldValue(w304);
	updateGroupFlag(w304);
	writeOrigin(w304,data);
	writeProlong(w304,data);
	writeContinue1(w304,data);
	writeContinue4(w304,data);
}

static bool checkAtom_1(W304 &w304)
{// 校验W304_Origin:Item.消息长度值
	return w304.var17446767732==3;

}

static bool setAtom_1(W304 &w304)
{// 设置W304_Origin:Item.消息长度值
	return w304.var17446767732=3;

}

static bool checkAtom_2(W304 &w304)
{// 校验W304_Origin:Item.消息长度值
	return w304.var17446767732==4;

}

static bool setAtom_2(W304 &w304)
{// 设置W304_Origin:Item.消息长度值
	return w304.var17446767732=4;

}

static bool checkConstraint_1(W304 &w304)
{// 计算 Constraint_1值
	return checkAtom_1(w304);

}

static bool setConstraint_1(W304 &w304)
{// 设置 Constraint_1值
	return setAtom_1(w304);

}

static bool checkConstraint_2(W304 &w304)
{// 计算 Constraint_2值
	return checkAtom_2(w304);

}

static bool setConstraint_2(W304 &w304)
{// 设置 Constraint_2值
	return setAtom_2(w304);

}

static QString checkVerify_1(W304 &w304,QString seq)
{
	return  (seq=="Seq_1"&&checkConstraint_2(w304) )?"Verify_1":"" ;

}

static bool setVerify_1(W304 &w304,QByteArray& data)
{
	  setConstraint_2(w304)  ;

	writeSeq_1(w304,data);
	return true;
}

static QString checkVerify_2(W304 &w304,QString seq)
{
	return  (seq=="Seq_2"&&checkConstraint_1(w304) )?"Verify_2":"" ;

}

static bool setVerify_2(W304 &w304,QByteArray& data)
{
	  setConstraint_1(w304)  ;

	writeSeq_2(w304,data);
	return true;
}

static QString checkVerify_3(W304 &w304,QString seq)
{
	return  (seq=="Seq_3"&&checkConstraint_1(w304) )?"Verify_3":"" ;

}

static bool setVerify_3(W304 &w304,QByteArray& data)
{
	  setConstraint_1(w304)  ;

	writeSeq_3(w304,data);
	return true;
}
static QString  Verifyw304Seq(W304&w304,QString seq)
{
	 for(int i=0;i < 3;i++)
	{
		switch (i)
		{
		case 0:                   
			{                   
				QString str = checkVerify_1(w304,seq);                   
				if(  str.isEmpty()==false)                    
					return str;
			}break;
		case 1:                   
			{                   
				QString str = checkVerify_2(w304,seq);                   
				if(  str.isEmpty()==false)                    
					return str;
			}break;
		case 2:                   
			{                   
				QString str = checkVerify_3(w304,seq);                   
				if(  str.isEmpty()==false)                    
					return str;
			}break;
		}
	}
	return "";
}
QString decodeMsg( uchar * pData, int len, W304 &w304){
	 int pos = 0;
	 unsigned char *pMsgData = pData;
	 int msgLens = len;
	 int index = 1;

// head
	checkPosition(pMsgData, msgLens,pos,2);
	w304.var1744751767 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	w304.var1744752768 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,5);
	w304.var1744759769 = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

	checkPosition(pMsgData, msgLens,pos,3);
	w304.var1744768770 = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	readOrigin(w304, pMsgData, msgLens, pos); 

	int ip= pos%8 == 0 ? 0:(8 - pos%8);
	pos+=ip;
		checkPosition(pMsgData, msgLens, pos, ip); 
	QStringList seqNum;
	seqNum.append("3");
	while( msgLens) 
	{                
		int temPos = pos;                
		unsigned char* pMsg = pMsgData;                
		int temLen = msgLens;                
		int by = 0;                
		int wordFlag = 0;                
		int wontinueWord = 0;
		by=readBits(pMsg, temLen, temPos ,2);

		temPos+=2;

		checkPosition(pMsg, temLen, temPos, 2); 
		wordFlag=readBits(pMsg, temLen, temPos ,2);

		temPos+=2;

		if(1==wordFlag)
		{ 
			 int key = wordFlag*10+index++; 
			 if (seqNum.indexOf(QString::number(key)) == -1)
				seqNum.append(QString::number(key));
			readW304(key,w304, pMsgData, msgLens, pos); 
			pos += pos%8 == 0 ? 0:(8 - pos%8);
			if(temLen==msgLens) break;
		 }
		else if(2==wordFlag)
		{ 
			wontinueWord=readBits(pMsg, temLen, temPos ,5);
 
			 int key = wordFlag*10+wontinueWord;
			  if (seqNum.indexOf(QString::number(key)) == -1)
				seqNum.append(QString::number(key));
			readW304(key,w304, pMsgData, msgLens, pos);
			pos += pos%8 == 0 ? 0:(8 - pos%8);
			if(temLen==msgLens) break; 
		 }
		else{
			 qDebug()<< __func__ <<" "<<__LINE__<<" msgLen:"<<msgLens <<"pos:"<<pos; break; }
		temPos = (pos%8 != 0)?pos/8 +1:pos/8;
		if((msgLens-temPos) == 0){break;}
	}
	QString strSeq,strSeqNum=seqNum.join("");                         
	 strSeq =checkW304SeqNum( strSeqNum);
	 if(strSeq.isEmpty()==true) return "";
	 QString verifySeq = Verifyw304Seq(w304, strSeq);
	 qDebug()<<"recv SeqNum:"<<  strSeqNum <<" recv Seq: " << strSeq << " recv verifySeq: " << verifySeq;  
	 if(verifySeq.isEmpty()==true)
	{
		return "";	}
	 return verifySeq;
}


void writeOrigin(W304&w304,QByteArray& data){
// head
	appendBits(w304.var1744751767,2,data,true,true);
	appendBits(w304.var1744752768,2,data,true);
	appendBits(w304.var1744759769,5,data,true);
	appendBits(w304.var1744768770,3,data,true);

// body
 
	int index =0; 
	 int count = 0;	appendBits(w304.var17446767732,3,data,true);
	appendBits(w304.var17446773733,15,data,true);
	appendBits(w304.var17446774734,8,data,true);
	appendBits(w304.var17446785735,3,data,true);
	appendBits(w304.var17446798736,5,data,true);
	appendBits(w304.var17446807737,6,data,true);
	appendBits(w304.var17446815738,6,data,true);
	appendBits(w304.var17446818739,14,data,true);

}

void writeProlong(W304&w304,QByteArray& data){
// head
	appendBits(w304.var1744769771,2,data,true);
	appendBits(w304.var1744779772,2,data,true);

// body
 
	int index =0; 
	 int count = 0;	appendBits(w304.var17446832740,1,data,true);
	appendBits(w304.var17446840741,16,data,true);
	appendBits(w304.var17446848742,26,data,true);
	appendBits(w304.var17446848743,25,data,true);

}

void writeContinue1(W304&w304,QByteArray& data){
// head
	appendBits(w304.var1744784773,2,data,true);
	appendBits(w304.var1744796776,2,data,true);
	appendBits(w304.var17447117779,5,data,true);

// body
 
	int index =0; 
	 int count = 0;	appendBits(w304.var17446920751,0,data,true);
	appendBits(w304.var17446864744,14,data,true);
	appendBits(w304.var17446864745,14,data,true);
	appendBits(w304.var17446880746,14,data,true);
	appendBits(w304.var17446880747,5,data,true);
	appendBits(w304.var17446897748,5,data,true);
	appendBits(w304.var17446903749,4,data,true);
	appendBits(w304.var17446912750,3,data,true);
	appendBits(w304.var17446920751,4,data,true);

}

void writeContinue2(W304&w304,QByteArray& data){
// head
	appendBits(w304.var1744784774,2,data,true);
	appendBits(w304.var1744796777,2,data,true);
	appendBits(w304.var17447117780,5,data,true);

// body
 
	int index =0; 
	 int count = 0;	appendBits(w304.var17446932752,18,data,true);
	appendBits(w304.var17446940753,18,data,true);
	appendBits(w304.var17446944754,18,data,true);
	appendBits(w304.var17446952755,3,data,true);
	appendBits(w304.var17446960756,3,data,true);
	appendBits(w304.var17446972757,3,data,true);

}

void writeContinue4(W304&w304,QByteArray& data){
// head
	appendBits(w304.var1744784775,2,data,true);
	appendBits(w304.var1744796778,2,data,true);
	appendBits(w304.var17447117781,5,data,true);

// body
 
	int index =0; 
	 int count = 0;	appendBits(w304.var1744743766,0,data,true);
	appendBits(w304.var17446978758,16,data,true);
	appendBits(w304.var17446988759,10,data,true);
	appendBits(w304.var17446993760,1,data,true);
	appendBits(w304.var174472761,1,data,true);
	appendBits(w304.var1744710762,2,data,true);
	appendBits(w304.var1744718763,15,data,true);
	appendBits(w304.var1744727764,2,data,true);
	appendBits(w304.var1744734765,2,data,true);
	appendBits(w304.var1744743766,18,data,true);

}
QString checkEncodeSeqNumber(W304 &w304)
{
	QString seqNum;
	int flag =0;
 
	int index =0; 
	 int count = 0;
	flag = 0;
	flag+=w304.var17446767732!=0;
	flag+=w304.var17446773733!=0;
	flag+=w304.var17446774734!=0;
	flag+=w304.var17446785735!=0;
	flag+=w304.var17446798736!=0;
	flag+=w304.var17446807737!=0;
	flag+=w304.var17446815738!=0;
	flag+=w304.var17446818739!=0;
	if(flag != 0){seqNum+="3";}
	flag = 0;
	flag+=w304.var17446832740!=0;
	flag+=w304.var17446840741!=0;
	flag+=w304.var17446848742!=22928799;
	flag+=w304.var17446848743!=0;
	if(flag != 0){seqNum+="11";}
	flag = 0;
	flag+=w304.var17446920751!=0;
	flag+=w304.var17446864744!=0;
	flag+=w304.var17446864745!=0;
	flag+=w304.var17446880746!=0;
	flag+=w304.var17446880747!=0;
	flag+=w304.var17446897748!=0;
	flag+=w304.var17446903749!=0;
	flag+=w304.var17446912750!=0;
	flag+=w304.var17446920751!=0;
	if(flag != 0){seqNum+="21";}
	flag = 0;
	flag+=w304.var17446932752!=0;
	flag+=w304.var17446940753!=0;
	flag+=w304.var17446944754!=0;
	flag+=w304.var17446952755!=0;
	flag+=w304.var17446960756!=0;
	flag+=w304.var17446972757!=0;
	if(flag != 0){seqNum+="22";}
	flag = 0;
	flag+=w304.var1744743766!=0;
	flag+=w304.var17446978758!=0;
	flag+=w304.var17446988759!=0;
	flag+=w304.var17446993760!=0;
	flag+=w304.var174472761!=0;
	flag+=w304.var1744710762!=0;
	flag+=w304.var1744718763!=0;
	flag+=w304.var1744727764!=0;
	flag+=w304.var1744734765!=0;
	flag+=w304.var1744743766!=0;
	if(flag != 0){seqNum+="24";}
	 return seqNum;}

void VerifyField(W304 &w304)
{
	int flag = 0; 
}
void updateFieldValue(W304 &w304)
{

	//bc1_备用位数为0,数据放到头hc1_**字段中
	 w304.var1744784773=w304.var17446920751;

	//bc4_备用位数为0,数据放到头hc4_**字段中
	 w304.var1744784775=w304.var1744743766;
}

void updateGroupFlag(W304 &w304)
{
}
void encodeMsg(QByteArray& data, W304 &w304){

	QString strSeqNum=checkEncodeSeqNumber(w304);
	QString temSeqNum;
	 temSeqNum="311212224";
	if(temSeqNum.contains(strSeqNum)){
	//Seq_1
		setVerify_1(w304,data);  
		return;
	}
	 temSeqNum="3112122";
	if(temSeqNum.contains(strSeqNum)){
	//Seq_2
		setVerify_2(w304,data);  
		return;
	}
	 temSeqNum="3112124";
	if(temSeqNum.contains(strSeqNum)){
	//Seq_3
		setVerify_3(w304,data);  
		return;
	}
	writeSeq_1(w304,data);
}


void readOrigin(S0_1&s0_1,uchar * & pMsgData, int & msgLens,int & pos){
// body
	checkPosition(pMsgData, msgLens,pos,1);
	s0_1.var1511111080 = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,2);
	s0_1.var1511111191 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,1);
	s0_1.var1511111242 = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,1);
	s0_1.var1511111333 = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,1);
	s0_1.var1511111414 = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,1);
	s0_1.var1511111505 = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,4);
	s0_1.var1511111576 = readBits(pMsgData, msgLens, pos ,4);
	pos += 4;

	checkPosition(pMsgData, msgLens,pos,4);
	s0_1.var1511111607 = readBits(pMsgData, msgLens, pos ,4);
	pos += 4;

	checkPosition(pMsgData, msgLens,pos,12);
	s0_1.var1511111688 = readBits(pMsgData, msgLens, pos ,12);
	pos += 12;

	checkPosition(pMsgData, msgLens,pos,2);
	s0_1.var1511111759 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,11);
	s0_1.var15111118310 = readBits(pMsgData, msgLens, pos ,11);
	pos += 11;

	checkPosition(pMsgData, msgLens,pos,13);
	s0_1.var15111119011 = readBits(pMsgData, msgLens, pos ,13);
	pos += 13;

	checkPosition(pMsgData, msgLens,pos,4);
	s0_1.var15111119012 = readBits(pMsgData, msgLens, pos ,4);
	pos += 4;

}

void readProlong(S0_1&s0_1,uchar * & pMsgData, int & msgLens,int & pos){
// head

	checkPosition(pMsgData, msgLens,pos,2);
	s0_1.var15111164272 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	s0_1.var15111165073 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

// body
	checkPosition(pMsgData, msgLens,pos,7);
	s0_1.var15111119913 = readBits(pMsgData, msgLens, pos ,7);
	pos += 7;

	checkPosition(pMsgData, msgLens,pos,1);
	s0_1.var15111120614 = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,1);
	s0_1.var15111121615 = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,1);
	s0_1.var15111122216 = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,1);
	s0_1.var15111123117 = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,1);
	s0_1.var15111123818 = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,4);
	s0_1.var15111124819 = readBits(pMsgData, msgLens, pos ,4);
	pos += 4;

	checkPosition(pMsgData, msgLens,pos,4);
	s0_1.var15111125520 = readBits(pMsgData, msgLens, pos ,4);
	pos += 4;

	checkPosition(pMsgData, msgLens,pos,1);
	s0_1.var15111126321 = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,24);
	s0_1.var15111127022 = readBits(pMsgData, msgLens, pos ,24);
	pos += 24;

	checkPosition(pMsgData, msgLens,pos,23);
	s0_1.var15111128023 = readBits(pMsgData, msgLens, pos ,23);
	pos += 23;

}

void readContinue1(S0_1&s0_1,uchar * & pMsgData, int & msgLens,int & pos){
// head

	checkPosition(pMsgData, msgLens,pos,2);
	s0_1.var15111165874 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	s0_1.var15111169379 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,5);
	s0_1.var15111172284 = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

// body
	checkPosition(pMsgData, msgLens,pos,12);
	s0_1.var15111128724 = readBits(pMsgData, msgLens, pos ,12);
	pos += 12;

	checkPosition(pMsgData, msgLens,pos,8);
	s0_1.var15111129725 = readBits(pMsgData, msgLens, pos ,8);
	pos += 8;

	checkPosition(pMsgData, msgLens,pos,3);
	s0_1.var15111130326 = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	checkPosition(pMsgData, msgLens,pos,5);
	s0_1.var15111135333 = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

	checkPosition(pMsgData, msgLens,pos,10);
	s0_1.var15111131928 = readBits(pMsgData, msgLens, pos ,10);
	pos += 10;

	checkPosition(pMsgData, msgLens,pos,2);
	s0_1.var15111135333 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	s0_1.var15111133330 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,1);
	s0_1.var15111133631 = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,1);
	s0_1.var15111134432 = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,1);
	s0_1.var15111135333 = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,9);
	s0_1.var15111136034 = readBits(pMsgData, msgLens, pos ,9);
	pos += 9;

	checkPosition(pMsgData, msgLens,pos,9);
	s0_1.var15111136935 = readBits_d(pMsgData, msgLens, pos ,9);
	pos += 9;

}

void readContinue2(S0_1&s0_1,uchar * & pMsgData, int & msgLens,int & pos){
// head

	checkPosition(pMsgData, msgLens,pos,2);
	s0_1.var15111165875 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	s0_1.var15111169380 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,5);
	s0_1.var15111172285 = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

// body
	checkPosition(pMsgData, msgLens,pos,4);
	s0_1.var15111137636 = readBits(pMsgData, msgLens, pos ,4);
	pos += 4;

	checkPosition(pMsgData, msgLens,pos,1);
	s0_1.var15111138437 = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,3);
	s0_1.var15111145747 = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	checkPosition(pMsgData, msgLens,pos,7);
	s0_1.var15111139939 = readBits(pMsgData, msgLens, pos ,7);
	pos += 7;

	checkPosition(pMsgData, msgLens,pos,1);
	s0_1.var15111140040 = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,4);
	s0_1.var15111140841 = readBits(pMsgData, msgLens, pos ,4);
	pos += 4;

	checkPosition(pMsgData, msgLens,pos,1);
	s0_1.var15111142242 = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,3);
	s0_1.var15111145747 = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	checkPosition(pMsgData, msgLens,pos,7);
	s0_1.var15111143244 = readBits(pMsgData, msgLens, pos ,7);
	pos += 7;

	checkPosition(pMsgData, msgLens,pos,1);
	s0_1.var15111144045 = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,1);
	s0_1.var15111144946 = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,16);
	s0_1.var15111145747 = readBits(pMsgData, msgLens, pos ,16);
	pos += 16;

	checkPosition(pMsgData, msgLens,pos,14);
	s0_1.var15111146548 = readBits(pMsgData, msgLens, pos ,14);
	pos += 14;

}

void readContinue3(S0_1&s0_1,uchar * & pMsgData, int & msgLens,int & pos){
// head

	checkPosition(pMsgData, msgLens,pos,2);
	s0_1.var15111165876 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	s0_1.var15111169381 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,5);
	s0_1.var15111172286 = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

// body
	checkPosition(pMsgData, msgLens,pos,4);
	s0_1.var15111147349 = readBits(pMsgData, msgLens, pos ,4);
	pos += 4;

	checkPosition(pMsgData, msgLens,pos,3);
	s0_1.var15111148350 = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	checkPosition(pMsgData, msgLens,pos,20);
	s0_1.var15111149051 = readBits(pMsgData, msgLens, pos ,20);
	pos += 20;

	checkPosition(pMsgData, msgLens,pos,20);
	s0_1.var15111149952 = readBits(pMsgData, msgLens, pos ,20);
	pos += 20;

	checkPosition(pMsgData, msgLens,pos,15);
	s0_1.var15111150653 = readBits(pMsgData, msgLens, pos ,15);
	pos += 15;

	checkPosition(pMsgData, msgLens,pos,1);
	s0_1.var15111151654 = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

}

void readContinue5(S0_1&s0_1,uchar * & pMsgData, int & msgLens,int & pos){
// head

	checkPosition(pMsgData, msgLens,pos,2);
	s0_1.var15111165877 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	s0_1.var15111169382 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,5);
	s0_1.var15111172287 = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

// body
	checkPosition(pMsgData, msgLens,pos,15);
	s0_1.var15111152255 = readBits(pMsgData, msgLens, pos ,15);
	pos += 15;

	checkPosition(pMsgData, msgLens,pos,15);
	s0_1.var15111153156 = readBits(pMsgData, msgLens, pos ,15);
	pos += 15;

	checkPosition(pMsgData, msgLens,pos,6);
	s0_1.var15111153857 = readBits(pMsgData, msgLens, pos ,6);
	pos += 6;

	checkPosition(pMsgData, msgLens,pos,8);
	s0_1.var15111154658 = readBits(pMsgData, msgLens, pos ,8);
	pos += 8;

	checkPosition(pMsgData, msgLens,pos,7);
	s0_1.var15111156259 = readBits(pMsgData, msgLens, pos ,7);
	pos += 7;

	checkPosition(pMsgData, msgLens,pos,1);
	s0_1.var15111156660 = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,11);
	s0_1.var15111157061 = readBits(pMsgData, msgLens, pos ,11);
	pos += 11;

}

void readContinue6(S0_1&s0_1,uchar * & pMsgData, int & msgLens,int & pos){
// head

	checkPosition(pMsgData, msgLens,pos,2);
	s0_1.var15111165878 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	s0_1.var15111169383 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,5);
	s0_1.var15111172288 = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

// body
	checkPosition(pMsgData, msgLens,pos,5);
	s0_1.var15111158362 = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

	checkPosition(pMsgData, msgLens,pos,6);
	s0_1.var15111158863 = readBits(pMsgData, msgLens, pos ,6);
	pos += 6;

	checkPosition(pMsgData, msgLens,pos,6);
	s0_1.var15111159964 = readBits(pMsgData, msgLens, pos ,6);
	pos += 6;

	checkPosition(pMsgData, msgLens,pos,10);
	s0_1.var15111160465 = readBits(pMsgData, msgLens, pos ,10);
	pos += 10;

	checkPosition(pMsgData, msgLens,pos,36);
	s0_1.var15111161066 = readBits(pMsgData, msgLens, pos ,36);
	pos += 36;

}
QString  checkS0_1SeqNum(QString strSeq)
{
	
	static QMap<QString,std::shared_ptr<MessCodeInfo>>  proSeqMap; 
	static int index = 0;                     
	if( 0 == index++ )
	{
     
	std::shared_ptr<MessCodeInfo> Seq_1(new MessCodeInfo); 
	Seq_1->icycle=0;
	Seq_1->itimes=1;
	Seq_1->strSeq="311";
	proSeqMap["Seq_1"] = Seq_1; 

	std::shared_ptr<MessCodeInfo> Seq_2(new MessCodeInfo); 
	Seq_2->icycle=0;
	Seq_2->itimes=1;
	Seq_2->strSeq="31121";
	proSeqMap["Seq_2"] = Seq_2; 

	std::shared_ptr<MessCodeInfo> Seq_3(new MessCodeInfo); 
	Seq_3->icycle=0;
	Seq_3->itimes=1;
	Seq_3->strSeq="31122";
	proSeqMap["Seq_3"] = Seq_3; 

	std::shared_ptr<MessCodeInfo> Seq_4(new MessCodeInfo); 
	Seq_4->icycle=0;
	Seq_4->itimes=1;
	Seq_4->strSeq="31123";
	proSeqMap["Seq_4"] = Seq_4; 

	std::shared_ptr<MessCodeInfo> Seq_5(new MessCodeInfo); 
	Seq_5->icycle=0;
	Seq_5->itimes=1;
	Seq_5->strSeq="31125";
	proSeqMap["Seq_5"] = Seq_5; 

	std::shared_ptr<MessCodeInfo> Seq_6(new MessCodeInfo); 
	Seq_6->icycle=0;
	Seq_6->itimes=1;
	Seq_6->strSeq="31126";
	proSeqMap["Seq_6"] = Seq_6; 

	}
	QString ret;
	auto ptr = proSeqMap.begin();        
	while (ptr != proSeqMap.end())        
	{            
		if (ptr.value()->strSeq == strSeq)            
		{                
			ret = ptr.key();                
			break;            
		}            
		ptr++;        
	}

	return ret;
        
}
void readOrigin(S0_1&s0_1,uchar * & pMsgData, int & msgLens,int & pos);
void readProlong(S0_1&s0_1,uchar * & pMsgData, int & msgLens,int & pos);
void readContinue1(S0_1&s0_1,uchar * & pMsgData, int & msgLens,int & pos);
void readContinue2(S0_1&s0_1,uchar * & pMsgData, int & msgLens,int & pos);
void readContinue3(S0_1&s0_1,uchar * & pMsgData, int & msgLens,int & pos);
void readContinue5(S0_1&s0_1,uchar * & pMsgData, int & msgLens,int & pos);
void readContinue6(S0_1&s0_1,uchar * & pMsgData, int & msgLens,int & pos);
void readS0_1(int key,S0_1 &s0_1,uchar * & pMsgData, int & msgLens,int & pos)
{
	switch (key)
{
	case 3:readOrigin(s0_1,pMsgData,msgLens,pos);break; 
	case 11:readProlong(s0_1,pMsgData,msgLens,pos);break;
	case 21:readContinue1(s0_1,pMsgData,msgLens,pos);break;
	case 22:readContinue2(s0_1,pMsgData,msgLens,pos);break;
	case 23:readContinue3(s0_1,pMsgData,msgLens,pos);break;
	case 25:readContinue5(s0_1,pMsgData,msgLens,pos);break;
	case 26:readContinue6(s0_1,pMsgData,msgLens,pos);break;
	default:
		break;
	}
}

void writeProlong(S0_1&s0_1,QByteArray& data);

void writeContinue2(S0_1&s0_1,QByteArray& data);

void writeContinue6(S0_1&s0_1,QByteArray& data);
void updateGroupFlag(S0_1 &s0_1);

void writeOrigin(S0_1&s0_1,QByteArray& data);
void updateFieldValue(S0_1 &s0_1);

void writeContinue3(S0_1&s0_1,QByteArray& data);

void writeContinue5(S0_1&s0_1,QByteArray& data);
void VerifyField(S0_1 &s0_1);

void writeContinue1(S0_1&s0_1,QByteArray& data);
static void writeSeq_1(S0_1&s0_1,QByteArray& data)
{

	VerifyField(s0_1);
	updateFieldValue(s0_1);
	updateGroupFlag(s0_1);
	writeOrigin(s0_1,data);
	writeProlong(s0_1,data);
}
static void writeSeq_2(S0_1&s0_1,QByteArray& data)
{

	VerifyField(s0_1);
	updateFieldValue(s0_1);
	updateGroupFlag(s0_1);
	writeOrigin(s0_1,data);
	writeProlong(s0_1,data);
	writeContinue1(s0_1,data);
}
static void writeSeq_3(S0_1&s0_1,QByteArray& data)
{

	VerifyField(s0_1);
	updateFieldValue(s0_1);
	updateGroupFlag(s0_1);
	writeOrigin(s0_1,data);
	writeProlong(s0_1,data);
	writeContinue2(s0_1,data);
}
static void writeSeq_4(S0_1&s0_1,QByteArray& data)
{

	VerifyField(s0_1);
	updateFieldValue(s0_1);
	updateGroupFlag(s0_1);
	writeOrigin(s0_1,data);
	writeProlong(s0_1,data);
	writeContinue3(s0_1,data);
}
static void writeSeq_5(S0_1&s0_1,QByteArray& data)
{

	VerifyField(s0_1);
	updateFieldValue(s0_1);
	updateGroupFlag(s0_1);
	writeOrigin(s0_1,data);
	writeProlong(s0_1,data);
	writeContinue5(s0_1,data);
}
static void writeSeq_6(S0_1&s0_1,QByteArray& data)
{

	VerifyField(s0_1);
	updateFieldValue(s0_1);
	updateGroupFlag(s0_1);
	writeOrigin(s0_1,data);
	writeProlong(s0_1,data);
	writeContinue6(s0_1,data);
}

int checkObjMaps(QString strVerify,QByteArray& data, S0_1 &s0_1)
{
	return 0;
}static QString  Verifys0_1Seq(S0_1&s0_1,QString seq)
{
	return " s0_1";
}
QString decodeMsg( uchar * pData, int len, S0_1 &s0_1){
	 int pos = 0;
	 unsigned char *pMsgData = pData;
	 int msgLens = len;
	 int index = 1;

// head
	checkPosition(pMsgData, msgLens,pos,2);
	s0_1.var15111162067 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	s0_1.var15111162668 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,5);
	s0_1.var15111162769 = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

	checkPosition(pMsgData, msgLens,pos,3);
	s0_1.var15111162770 = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	checkPosition(pMsgData, msgLens,pos,3);
	s0_1.var15111164271 = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	readOrigin(s0_1, pMsgData, msgLens, pos); 

	int ip= pos%8 == 0 ? 0:(8 - pos%8);
	pos+=ip;
		checkPosition(pMsgData, msgLens, pos, ip); 
	QStringList seqNum;
	seqNum.append("3");
	while( msgLens) 
	{                
		int temPos = pos;                
		unsigned char* pMsg = pMsgData;                
		int temLen = msgLens;                
		int by = 0;                
		int wordFlag = 0;                
		int wontinueWord = 0;
		by=readBits(pMsg, temLen, temPos ,2);

		temPos+=2;

		checkPosition(pMsg, temLen, temPos, 2); 
		wordFlag=readBits(pMsg, temLen, temPos ,2);

		temPos+=2;

		if(1==wordFlag)
		{ 
			 int key = wordFlag*10+index++; 
			 if (seqNum.indexOf(QString::number(key)) == -1)
				seqNum.append(QString::number(key));
			readS0_1(key,s0_1, pMsgData, msgLens, pos); 
			pos += pos%8 == 0 ? 0:(8 - pos%8);
			if(temLen==msgLens) break;
		 }
		else if(2==wordFlag)
		{ 
			wontinueWord=readBits(pMsg, temLen, temPos ,5);
 
			 int key = wordFlag*10+wontinueWord;
			  if (seqNum.indexOf(QString::number(key)) == -1)
				seqNum.append(QString::number(key));
			readS0_1(key,s0_1, pMsgData, msgLens, pos);
			pos += pos%8 == 0 ? 0:(8 - pos%8);
			if(temLen==msgLens) break; 
		 }
		else{
			 qDebug()<< __func__ <<" "<<__LINE__<<" msgLen:"<<msgLens <<"pos:"<<pos; break; }
		temPos = (pos%8 != 0)?pos/8 +1:pos/8;
		if((msgLens-temPos) == 0){break;}
	}
	QString strSeq,strSeqNum=seqNum.join("");                         
	 strSeq =checkS0_1SeqNum( strSeqNum);
	 if(strSeq.isEmpty()==true) return "";
	 QString verifySeq = Verifys0_1Seq(s0_1, strSeq);
	 qDebug()<<"recv SeqNum:"<<  strSeqNum <<" recv Seq: " << strSeq << " recv verifySeq: " << verifySeq;  
	 if(verifySeq.isEmpty()==true)
	{
		return "";	}
	 return verifySeq;
}


void writeOrigin(S0_1&s0_1,QByteArray& data){
// head
	appendBits(s0_1.var15111162067,2,data,true,true);
	appendBits(s0_1.var15111162668,2,data,true);
	appendBits(s0_1.var15111162769,5,data,true);
	appendBits(s0_1.var15111162770,3,data,true);
	appendBits(s0_1.var15111164271,3,data,true);

// body
 
	int index =0; 
	 int count = 0;	appendBits(s0_1.var1511111080,1,data,true);
	appendBits(s0_1.var1511111191,2,data,true);
	appendBits(s0_1.var1511111242,1,data,true);
	appendBits(s0_1.var1511111333,1,data,true);
	appendBits(s0_1.var1511111414,1,data,true);
	appendBits(s0_1.var1511111505,1,data,true);
	appendBits(s0_1.var1511111576,4,data,true);
	appendBits(s0_1.var1511111607,4,data,true);
	appendBits(s0_1.var1511111688,12,data,true);
	appendBits(s0_1.var1511111759,2,data,true);
	appendBits(s0_1.var15111118310,11,data,true);
	appendBits(s0_1.var15111119011,13,data,true);
	appendBits(s0_1.var15111119012,4,data,true);

}

void writeProlong(S0_1&s0_1,QByteArray& data){
// head
	appendBits(s0_1.var15111164272,2,data,true);
	appendBits(s0_1.var15111165073,2,data,true);

// body
 
	int index =0; 
	 int count = 0;	appendBits(s0_1.var15111119913,7,data,true);
	appendBits(s0_1.var15111120614,1,data,true);
	appendBits(s0_1.var15111121615,1,data,true);
	appendBits(s0_1.var15111122216,1,data,true);
	appendBits(s0_1.var15111123117,1,data,true);
	appendBits(s0_1.var15111123818,1,data,true);
	appendBits(s0_1.var15111124819,4,data,true);
	appendBits(s0_1.var15111125520,4,data,true);
	appendBits(s0_1.var15111126321,1,data,true);
	appendBits(s0_1.var15111127022,24,data,true);
	appendBits(s0_1.var15111128023,23,data,true);

}

void writeContinue1(S0_1&s0_1,QByteArray& data){
// head
	appendBits(s0_1.var15111165874,2,data,true);
	appendBits(s0_1.var15111169379,2,data,true);
	appendBits(s0_1.var15111172284,5,data,true);

// body
 
	int index =0; 
	 int count = 0;	appendBits(s0_1.var15111128724,12,data,true);
	appendBits(s0_1.var15111129725,8,data,true);
	appendBits(s0_1.var15111130326,3,data,true);
	appendBits(s0_1.var15111135333,5,data,true);
	appendBits(s0_1.var15111131928,10,data,true);
	appendBits(s0_1.var15111135333,2,data,true);
	appendBits(s0_1.var15111133330,2,data,true);
	appendBits(s0_1.var15111133631,1,data,true);
	appendBits(s0_1.var15111134432,1,data,true);
	appendBits(s0_1.var15111135333,1,data,true);
	appendBits(s0_1.var15111136034,9,data,true);
	appendBits_d(s0_1.var15111136935,9,data,true);

}

void writeContinue2(S0_1&s0_1,QByteArray& data){
// head
	appendBits(s0_1.var15111165875,2,data,true);
	appendBits(s0_1.var15111169380,2,data,true);
	appendBits(s0_1.var15111172285,5,data,true);

// body
 
	int index =0; 
	 int count = 0;	appendBits(s0_1.var15111137636,4,data,true);
	appendBits(s0_1.var15111138437,1,data,true);
	appendBits(s0_1.var15111145747,3,data,true);
	appendBits(s0_1.var15111139939,7,data,true);
	appendBits(s0_1.var15111140040,1,data,true);
	appendBits(s0_1.var15111140841,4,data,true);
	appendBits(s0_1.var15111142242,1,data,true);
	appendBits(s0_1.var15111145747,3,data,true);
	appendBits(s0_1.var15111143244,7,data,true);
	appendBits(s0_1.var15111144045,1,data,true);
	appendBits(s0_1.var15111144946,1,data,true);
	appendBits(s0_1.var15111145747,16,data,true);
	appendBits(s0_1.var15111146548,14,data,true);

}

void writeContinue3(S0_1&s0_1,QByteArray& data){
// head
	appendBits(s0_1.var15111165876,2,data,true);
	appendBits(s0_1.var15111169381,2,data,true);
	appendBits(s0_1.var15111172286,5,data,true);

// body
 
	int index =0; 
	 int count = 0;	appendBits(s0_1.var15111147349,4,data,true);
	appendBits(s0_1.var15111148350,3,data,true);
	appendBits(s0_1.var15111149051,20,data,true);
	appendBits(s0_1.var15111149952,20,data,true);
	appendBits(s0_1.var15111150653,15,data,true);
	appendBits(s0_1.var15111151654,1,data,true);

}

void writeContinue5(S0_1&s0_1,QByteArray& data){
// head
	appendBits(s0_1.var15111165877,2,data,true);
	appendBits(s0_1.var15111169382,2,data,true);
	appendBits(s0_1.var15111172287,5,data,true);

// body
 
	int index =0; 
	 int count = 0;	appendBits(s0_1.var15111152255,15,data,true);
	appendBits(s0_1.var15111153156,15,data,true);
	appendBits(s0_1.var15111153857,6,data,true);
	appendBits(s0_1.var15111154658,8,data,true);
	appendBits(s0_1.var15111156259,7,data,true);
	appendBits(s0_1.var15111156660,1,data,true);
	appendBits(s0_1.var15111157061,11,data,true);

}

void writeContinue6(S0_1&s0_1,QByteArray& data){
// head
	appendBits(s0_1.var15111165878,2,data,true);
	appendBits(s0_1.var15111169383,2,data,true);
	appendBits(s0_1.var15111172288,5,data,true);

// body
 
	int index =0; 
	 int count = 0;	appendBits(s0_1.var15111158362,5,data,true);
	appendBits(s0_1.var15111158863,6,data,true);
	appendBits(s0_1.var15111159964,6,data,true);
	appendBits(s0_1.var15111160465,10,data,true);
	appendBits(s0_1.var15111161066,36,data,true);

}
QString checkEncodeSeqNumber(S0_1 &s0_1)
{
	QString seqNum;
	int flag =0;
 
	int index =0; 
	 int count = 0;
	flag = 0;
	flag+=s0_1.var1511111080!=0;
	flag+=s0_1.var1511111191!=0;
	flag+=s0_1.var1511111242!=0;
	flag+=s0_1.var1511111333!=0;
	flag+=s0_1.var1511111414!=0;
	flag+=s0_1.var1511111505!=0;
	flag+=s0_1.var1511111576!=0;
	flag+=s0_1.var1511111607!=0;
	flag+=s0_1.var1511111688!=0;
	flag+=s0_1.var1511111759!=0;
	flag+=s0_1.var15111118310!=0;
	flag+=s0_1.var15111119011!=0;
	flag+=s0_1.var15111119012!=0;
	if(flag != 0){seqNum+="3";}
	flag = 0;
	flag+=s0_1.var15111119913!=0;
	flag+=s0_1.var15111120614!=0;
	flag+=s0_1.var15111121615!=0;
	flag+=s0_1.var15111122216!=0;
	flag+=s0_1.var15111123117!=0;
	flag+=s0_1.var15111123818!=0;
	flag+=s0_1.var15111124819!=0;
	flag+=s0_1.var15111125520!=0;
	flag+=s0_1.var15111126321!=0;
	flag+=s0_1.var15111127022!=0;
	flag+=s0_1.var15111128023!=0;
	if(flag != 0){seqNum+="11";}
	flag = 0;
	flag+=s0_1.var15111128724!=0;
	flag+=s0_1.var15111129725!=0;
	flag+=s0_1.var15111130326!=0;
	flag+=s0_1.var15111135333!=0;
	flag+=s0_1.var15111131928!=0;
	flag+=s0_1.var15111135333!=0;
	flag+=s0_1.var15111133330!=0;
	flag+=s0_1.var15111133631!=0;
	flag+=s0_1.var15111134432!=0;
	flag+=s0_1.var15111135333!=0;
	flag+=s0_1.var15111136034!=0;
	flag+=s0_1.var15111136935!=0;
	if(flag != 0){seqNum+="21";}
	flag = 0;
	flag+=s0_1.var15111137636!=0;
	flag+=s0_1.var15111138437!=0;
	flag+=s0_1.var15111145747!=0;
	flag+=s0_1.var15111139939!=0;
	flag+=s0_1.var15111140040!=0;
	flag+=s0_1.var15111140841!=0;
	flag+=s0_1.var15111142242!=0;
	flag+=s0_1.var15111145747!=0;
	flag+=s0_1.var15111143244!=0;
	flag+=s0_1.var15111144045!=0;
	flag+=s0_1.var15111144946!=0;
	flag+=s0_1.var15111145747!=0;
	flag+=s0_1.var15111146548!=0;
	if(flag != 0){seqNum+="22";}
	flag = 0;
	flag+=s0_1.var15111147349!=0;
	flag+=s0_1.var15111148350!=0;
	flag+=s0_1.var15111149051!=0;
	flag+=s0_1.var15111149952!=0;
	flag+=s0_1.var15111150653!=0;
	flag+=s0_1.var15111151654!=0;
	if(flag != 0){seqNum+="23";}
	flag = 0;
	flag+=s0_1.var15111152255!=0;
	flag+=s0_1.var15111153156!=0;
	flag+=s0_1.var15111153857!=0;
	flag+=s0_1.var15111154658!=0;
	flag+=s0_1.var15111156259!=0;
	flag+=s0_1.var15111156660!=1;
	flag+=s0_1.var15111157061!=0;
	if(flag != 0){seqNum+="25";}
	flag = 0;
	flag+=s0_1.var15111158362!=0;
	flag+=s0_1.var15111158863!=0;
	flag+=s0_1.var15111159964!=0;
	flag+=s0_1.var15111160465!=0;
	flag+=s0_1.var15111161066!=0;
	if(flag != 0){seqNum+="26";}
	 return seqNum;}

void VerifyField(S0_1 &s0_1)
{
	int flag = 0; 
}
void updateFieldValue(S0_1 &s0_1)
{
}

void updateGroupFlag(S0_1 &s0_1)
{
}
void encodeMsg(QByteArray& data, S0_1 &s0_1){

	QString strSeqNum=checkEncodeSeqNumber(s0_1);
	QString temSeqNum;
	 temSeqNum="311";
	if(temSeqNum.contains(strSeqNum)){
	//Seq_1
		writeSeq_1(s0_1,data); 
		return;
	}
	 temSeqNum="31121";
	if(temSeqNum.contains(strSeqNum)){
	//Seq_2
		writeSeq_2(s0_1,data); 
		return;
	}
	 temSeqNum="31122";
	if(temSeqNum.contains(strSeqNum)){
	//Seq_3
		writeSeq_3(s0_1,data); 
		return;
	}
	 temSeqNum="31123";
	if(temSeqNum.contains(strSeqNum)){
	//Seq_4
		writeSeq_4(s0_1,data); 
		return;
	}
	 temSeqNum="31125";
	if(temSeqNum.contains(strSeqNum)){
	//Seq_5
		writeSeq_5(s0_1,data); 
		return;
	}
	 temSeqNum="31126";
	if(temSeqNum.contains(strSeqNum)){
	//Seq_6
		writeSeq_6(s0_1,data); 
		return;
	}
	writeSeq_1(s0_1,data);
}

QString  checkW204SeqNum(QString strSeq)
{
	
	static QMap<QString,std::shared_ptr<MessCodeInfo>>  proSeqMap; 
	static int index = 0;                     
	if( 0 == index++ )
	{
     
	std::shared_ptr<MessCodeInfo> Seq_1(new MessCodeInfo); 
	Seq_1->icycle=0;
	Seq_1->itimes=1;
	Seq_1->strSeq="311212223242526";
	proSeqMap["Seq_1"] = Seq_1; 

	}
	QString ret;
	auto ptr = proSeqMap.begin();        
	while (ptr != proSeqMap.end())        
	{            
		if (ptr.value()->strSeq == strSeq)            
		{                
			ret = ptr.key();                
			break;            
		}            
		ptr++;        
	}

	return ret;
        
}
void readOrigin(W204&w204,uchar * & pMsgData, int & msgLens,int & pos);
void readProlong(W204&w204,uchar * & pMsgData, int & msgLens,int & pos);
void readContinue1(W204&w204,uchar * & pMsgData, int & msgLens,int & pos);
void readContinue2(W204&w204,uchar * & pMsgData, int & msgLens,int & pos);
void readContinue3(W204&w204,uchar * & pMsgData, int & msgLens,int & pos);
void readContinue4(W204&w204,uchar * & pMsgData, int & msgLens,int & pos);
void readContinue5(W204&w204,uchar * & pMsgData, int & msgLens,int & pos);
void readContinue6(W204&w204,uchar * & pMsgData, int & msgLens,int & pos);
void readW204(int key,W204 &w204,uchar * & pMsgData, int & msgLens,int & pos)
{
	switch (key)
{
	case 3:readOrigin(w204,pMsgData,msgLens,pos);break; 
	case 11:readProlong(w204,pMsgData,msgLens,pos);break;
	case 21:readContinue1(w204,pMsgData,msgLens,pos);break;
	case 22:readContinue2(w204,pMsgData,msgLens,pos);break;
	case 23:readContinue3(w204,pMsgData,msgLens,pos);break;
	case 24:readContinue4(w204,pMsgData,msgLens,pos);break;
	case 25:readContinue5(w204,pMsgData,msgLens,pos);break;
	case 26:readContinue6(w204,pMsgData,msgLens,pos);break;
	default:
		break;
	}
}

void writeContinue6(W204&w204,QByteArray& data);

void writeContinue4(W204&w204,QByteArray& data);
void updateFieldValue(W204 &w204);

void writeProlong(W204&w204,QByteArray& data);

void writeContinue3(W204&w204,QByteArray& data);
void VerifyField(W204 &w204);

void writeContinue5(W204&w204,QByteArray& data);

void writeContinue1(W204&w204,QByteArray& data);
void updateGroupFlag(W204 &w204);

void writeOrigin(W204&w204,QByteArray& data);

void writeContinue2(W204&w204,QByteArray& data);
static void writeSeq_1(W204&w204,QByteArray& data)
{

	VerifyField(w204);
	updateFieldValue(w204);
	updateGroupFlag(w204);
	writeOrigin(w204,data);
	writeProlong(w204,data);
	writeContinue1(w204,data);
	writeContinue2(w204,data);
	writeContinue3(w204,data);
	writeContinue4(w204,data);
	writeContinue5(w204,data);
	writeContinue6(w204,data);
}

void readOrigin(W204&w204,uchar * & pMsgData, int & msgLens,int & pos){
// body
	checkPosition(pMsgData, msgLens,pos,3);
	w204.var17447134782 = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	checkPosition(pMsgData, msgLens,pos,15);
	w204.var17447142783 = readBits(pMsgData, msgLens, pos ,15);
	pos += 15;

	checkPosition(pMsgData, msgLens,pos,2);
	w204.var17447142784 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,3);
	w204.var17447151785 = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	checkPosition(pMsgData, msgLens,pos,3);
	w204.var17447159786 = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	checkPosition(pMsgData, msgLens,pos,3);
	w204.var17447163787 = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	checkPosition(pMsgData, msgLens,pos,5);
	w204.var17447166788 = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

	checkPosition(pMsgData, msgLens,pos,6);
	w204.var17447174789 = readBits(pMsgData, msgLens, pos ,6);
	pos += 6;

	checkPosition(pMsgData, msgLens,pos,6);
	w204.var17447174790 = readBits(pMsgData, msgLens, pos ,6);
	pos += 6;

	checkPosition(pMsgData, msgLens,pos,14);
	w204.var17447185791 = readBits(pMsgData, msgLens, pos ,14);
	pos += 14;

}

void readProlong(W204&w204,uchar * & pMsgData, int & msgLens,int & pos){
// head

	checkPosition(pMsgData, msgLens,pos,2);
	w204.var17447677862 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	w204.var17447685863 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

// body
//位数为0的字段，用头中的**字段值代替
	w204.var17447191792 = w204.var17447677862;
	checkPosition(pMsgData, msgLens,pos,1);
	w204.var17447191793 = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,16);
	w204.var17447201794 = readBits(pMsgData, msgLens, pos ,16);
	pos += 16;

	checkPosition(pMsgData, msgLens,pos,26);
	w204.var17447207795 = readBits(pMsgData, msgLens, pos ,26);
	pos += 26;

	checkPosition(pMsgData, msgLens,pos,25);
	w204.var17447213796 = readBits(pMsgData, msgLens, pos ,25);
	pos += 25;

}

void readContinue1(W204&w204,uchar * & pMsgData, int & msgLens,int & pos){
// head

	checkPosition(pMsgData, msgLens,pos,2);
	w204.var17447694864 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	w204.var17447727870 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,5);
	w204.var17447760876 = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

// body
//位数为0的字段，用头中的**字段值代替
	w204.var17447216797 = w204.var17447694864;
	checkPosition(pMsgData, msgLens,pos,14);
	w204.var17447226798 = readBits(pMsgData, msgLens, pos ,14);
	pos += 14;

	checkPosition(pMsgData, msgLens,pos,14);
	w204.var17447232799 = readBits(pMsgData, msgLens, pos ,14);
	pos += 14;

	checkPosition(pMsgData, msgLens,pos,14);
	w204.var17447240800 = readBits(pMsgData, msgLens, pos ,14);
	pos += 14;
	if( 1 == w204.var17447191793){

	checkPosition(pMsgData, msgLens,pos,5);
	w204.var17447248801 = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;
	}

	if( 0 == w204.var17447191793){

	checkPosition(pMsgData, msgLens,pos,5);
	w204.var17447248802 = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;
	}


	checkPosition(pMsgData, msgLens,pos,5);
	w204.var17447264803 = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

	checkPosition(pMsgData, msgLens,pos,4);
	w204.var17447267804 = readBits(pMsgData, msgLens, pos ,4);
	pos += 4;

	checkPosition(pMsgData, msgLens,pos,3);
	w204.var17447281805 = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	checkPosition(pMsgData, msgLens,pos,2);
	w204.var17447284806 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	w204.var17447296807 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

}

void readContinue2(W204&w204,uchar * & pMsgData, int & msgLens,int & pos){
// head

	checkPosition(pMsgData, msgLens,pos,2);
	w204.var17447694865 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	w204.var17447727871 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,5);
	w204.var17447760877 = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

// body
//位数为0的字段，用头中的**字段值代替
	w204.var17447304808 = w204.var17447694865;
	checkPosition(pMsgData, msgLens,pos,22);
	w204.var17447315809 = readBits(pMsgData, msgLens, pos ,22);
	pos += 22;

	checkPosition(pMsgData, msgLens,pos,19);
	w204.var17447320810 = readBits(pMsgData, msgLens, pos ,19);
	pos += 19;

	checkPosition(pMsgData, msgLens,pos,18);
	w204.var17447324811 = readBits(pMsgData, msgLens, pos ,18);
	pos += 18;

	checkPosition(pMsgData, msgLens,pos,4);
	w204.var17447304808 = readBits(pMsgData, msgLens, pos ,4);
	pos += 4;

}

void readContinue3(W204&w204,uchar * & pMsgData, int & msgLens,int & pos){
// head

	checkPosition(pMsgData, msgLens,pos,2);
	w204.var17447694866 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	w204.var17447727872 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,5);
	w204.var17447760878 = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

// body
//位数为0的字段，用头中的**字段值代替
	w204.var17447330813 = w204.var17447694866;
	checkPosition(pMsgData, msgLens,pos,18);
	w204.var17447330814 = readBits(pMsgData, msgLens, pos ,18);
	pos += 18;

	checkPosition(pMsgData, msgLens,pos,18);
	w204.var17447340815 = readBits(pMsgData, msgLens, pos ,18);
	pos += 18;

	checkPosition(pMsgData, msgLens,pos,18);
	w204.var17447346816 = readBits(pMsgData, msgLens, pos ,18);
	pos += 18;

	checkPosition(pMsgData, msgLens,pos,3);
	w204.var17447353817 = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	checkPosition(pMsgData, msgLens,pos,3);
	w204.var17447361818 = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	checkPosition(pMsgData, msgLens,pos,3);
	w204.var17447361819 = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

}

void readContinue4(W204&w204,uchar * & pMsgData, int & msgLens,int & pos){
// head

	checkPosition(pMsgData, msgLens,pos,2);
	w204.var17447694867 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	w204.var17447727873 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,5);
	w204.var17447760879 = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

// body
//位数为0的字段，用头中的**字段值代替
	w204.var17447377820 = w204.var17447694867;
	checkPosition(pMsgData, msgLens,pos,1);
	w204.var17447379821 = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;
	if( 1 == w204.var17447281805){

	checkPosition(pMsgData, msgLens,pos,8);
	w204.var17447385822 = readBits(pMsgData, msgLens, pos ,8);
	pos += 8;

	checkPosition(pMsgData, msgLens,pos,4);
	w204.var17447377820 = readBits(pMsgData, msgLens, pos ,4);
	pos += 4;
	}

	if( 2 == w204.var17447281805){

	checkPosition(pMsgData, msgLens,pos,12);
	w204.var17447396824 = readBits(pMsgData, msgLens, pos ,12);
	pos += 12;
	}


	checkPosition(pMsgData, msgLens,pos,6);
	w204.var17447402825 = readBits(pMsgData, msgLens, pos ,6);
	pos += 6;

	checkPosition(pMsgData, msgLens,pos,6);
	w204.var17447413826 = readBits(pMsgData, msgLens, pos ,6);
	pos += 6;

	checkPosition(pMsgData, msgLens,pos,4);
	w204.var17447377820 = readBits(pMsgData, msgLens, pos ,4);
	pos += 4;

	checkPosition(pMsgData, msgLens,pos,2);
	w204.var17447419828 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,7);
	w204.var17447429829 = readBits(pMsgData, msgLens, pos ,7);
	pos += 7;

	checkPosition(pMsgData, msgLens,pos,7);
	w204.var17447434830 = readBits(pMsgData, msgLens, pos ,7);
	pos += 7;
	if( 0 == w204.var17447379821){

	checkPosition(pMsgData, msgLens,pos,7);
	w204.var17447443831 = readBits(pMsgData, msgLens, pos ,7);
	pos += 7;
	}

	if( 1 == w204.var17447379821){

	checkPosition(pMsgData, msgLens,pos,7);
	w204.var17447451832 = readBits(pMsgData, msgLens, pos ,7);
	pos += 7;
	}


	checkPosition(pMsgData, msgLens,pos,4);
	w204.var17447459833 = readBits(pMsgData, msgLens, pos ,4);
	pos += 4;

	checkPosition(pMsgData, msgLens,pos,3);
	w204.var17447459834 = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	checkPosition(pMsgData, msgLens,pos,2);
	w204.var17447467835 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	w204.var17447475836 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

}

void readContinue5(W204&w204,uchar * & pMsgData, int & msgLens,int & pos){
// head

	checkPosition(pMsgData, msgLens,pos,2);
	w204.var17447694868 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	w204.var17447727874 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,5);
	w204.var17447760880 = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

// body
//位数为0的字段，用头中的**字段值代替
	w204.var17447483837 = w204.var17447694868;
	checkPosition(pMsgData, msgLens,pos,1);
	w204.var202509201216441 = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,6);
	w204.var17447499839 = readBits(pMsgData, msgLens, pos ,6);
	pos += 6;

	checkPosition(pMsgData, msgLens,pos,6);
	w204.var17447507840 = readBits(pMsgData, msgLens, pos ,6);
	pos += 6;

	checkPosition(pMsgData, msgLens,pos,6);
	w204.var17447515841 = readBits(pMsgData, msgLens, pos ,6);
	pos += 6;

	checkPosition(pMsgData, msgLens,pos,6);
	w204.var17447523842 = readBits(pMsgData, msgLens, pos ,6);
	pos += 6;

	checkPosition(pMsgData, msgLens,pos,6);
	w204.var17447531843 = readBits(pMsgData, msgLens, pos ,6);
	pos += 6;

	checkPosition(pMsgData, msgLens,pos,8);
	w204.var17447540844 = readBits(pMsgData, msgLens, pos ,8);
	pos += 8;

	checkPosition(pMsgData, msgLens,pos,8);
	w204.var17447548845 = readBits(pMsgData, msgLens, pos ,8);
	pos += 8;

	checkPosition(pMsgData, msgLens,pos,8);
	w204.var17447548846 = readBits(pMsgData, msgLens, pos ,8);
	pos += 8;

	checkPosition(pMsgData, msgLens,pos,8);
	w204.var17447564847 = readBits(pMsgData, msgLens, pos ,8);
	pos += 8;

}

void readContinue6(W204&w204,uchar * & pMsgData, int & msgLens,int & pos){
// head

	checkPosition(pMsgData, msgLens,pos,2);
	w204.var17447694869 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	w204.var17447727875 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,5);
	w204.var17447760881 = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

// body
//位数为0的字段，用头中的**字段值代替
	w204.var17447567848 = w204.var17447694869;
	checkPosition(pMsgData, msgLens,pos,3);
	w204.var17447579849 = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	checkPosition(pMsgData, msgLens,pos,5);
	w204.var17447580850 = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

	checkPosition(pMsgData, msgLens,pos,14);
	w204.var17447595851 = readBits(pMsgData, msgLens, pos ,14);
	pos += 14;

	checkPosition(pMsgData, msgLens,pos,6);
	w204.var17447604852 = readBits(pMsgData, msgLens, pos ,6);
	pos += 6;

	checkPosition(pMsgData, msgLens,pos,6);
	w204.var17447612853 = readBits(pMsgData, msgLens, pos ,6);
	pos += 6;

	checkPosition(pMsgData, msgLens,pos,6);
	w204.var17447620854 = readBits(pMsgData, msgLens, pos ,6);
	pos += 6;

	checkPosition(pMsgData, msgLens,pos,6);
	w204.var17447628855 = readBits(pMsgData, msgLens, pos ,6);
	pos += 6;

	checkPosition(pMsgData, msgLens,pos,6);
	w204.var17447641856 = readBits(pMsgData, msgLens, pos ,6);
	pos += 6;

	checkPosition(pMsgData, msgLens,pos,11);
	w204.var17447649857 = readBits(pMsgData, msgLens, pos ,11);
	pos += 11;

}

static bool checkAtom_1(W204 &w204)
{// 校验W204_Origin:Item.消息长度值
	return w204.var17447134782==7;

}

static bool setAtom_1(W204 &w204)
{// 设置W204_Origin:Item.消息长度值
	return w204.var17447134782=7;

}

static bool checkConstraint_1(W204 &w204)
{// 计算 Constraint_1值
	return checkAtom_1(w204);

}

static bool setConstraint_1(W204 &w204)
{// 设置 Constraint_1值
	return setAtom_1(w204);

}

static QString checkVerify_1(W204 &w204,QString seq)
{
	return  (seq=="Seq_1"&&checkConstraint_1(w204) )?"Verify_1":"" ;

}

static bool setVerify_1(W204 &w204,QByteArray& data)
{
	  setConstraint_1(w204)  ;

	writeSeq_1(w204,data);
	return true;
}
static QString  Verifyw204Seq(W204&w204,QString seq)
{
	 for(int i=0;i < 1;i++)
	{
		switch (i)
		{
		case 0:                   
			{                   
				QString str = checkVerify_1(w204,seq);                   
				if(  str.isEmpty()==false)                    
					return str;
			}break;
		}
	}
	return "";
}
QString decodeMsg( uchar * pData, int len, W204 &w204){
	 int pos = 0;
	 unsigned char *pMsgData = pData;
	 int msgLens = len;
	 int index = 1;

// head
	checkPosition(pMsgData, msgLens,pos,2);
	w204.var17447653858 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	w204.var17447664859 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,5);
	w204.var17447669860 = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

	checkPosition(pMsgData, msgLens,pos,3);
	w204.var17447677861 = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	readOrigin(w204, pMsgData, msgLens, pos); 

	int ip= pos%8 == 0 ? 0:(8 - pos%8);
	pos+=ip;
		checkPosition(pMsgData, msgLens, pos, ip); 
	QStringList seqNum;
	seqNum.append("3");
	while( msgLens) 
	{                
		int temPos = pos;                
		unsigned char* pMsg = pMsgData;                
		int temLen = msgLens;                
		int by = 0;                
		int wordFlag = 0;                
		int wontinueWord = 0;
		by=readBits(pMsg, temLen, temPos ,2);

		temPos+=2;

		checkPosition(pMsg, temLen, temPos, 2); 
		wordFlag=readBits(pMsg, temLen, temPos ,2);

		temPos+=2;

		if(1==wordFlag)
		{ 
			 int key = wordFlag*10+index++; 
			 if (seqNum.indexOf(QString::number(key)) == -1)
				seqNum.append(QString::number(key));
			readW204(key,w204, pMsgData, msgLens, pos); 
			pos += pos%8 == 0 ? 0:(8 - pos%8);
			if(temLen==msgLens) break;
		 }
		else if(2==wordFlag)
		{ 
			wontinueWord=readBits(pMsg, temLen, temPos ,5);
 
			 int key = wordFlag*10+wontinueWord;
			  if (seqNum.indexOf(QString::number(key)) == -1)
				seqNum.append(QString::number(key));
			readW204(key,w204, pMsgData, msgLens, pos);
			pos += pos%8 == 0 ? 0:(8 - pos%8);
			if(temLen==msgLens) break; 
		 }
		else{
			 qDebug()<< __func__ <<" "<<__LINE__<<" msgLen:"<<msgLens <<"pos:"<<pos; break; }
		temPos = (pos%8 != 0)?pos/8 +1:pos/8;
		if((msgLens-temPos) == 0){break;}
	}
	QString strSeq,strSeqNum=seqNum.join("");                         
	 strSeq =checkW204SeqNum( strSeqNum);
	 if(strSeq.isEmpty()==true) return "";
	 QString verifySeq = Verifyw204Seq(w204, strSeq);
	 qDebug()<<"recv SeqNum:"<<  strSeqNum <<" recv Seq: " << strSeq << " recv verifySeq: " << verifySeq;  
	 if(verifySeq.isEmpty()==true)
	{
		return "";	}
	 return verifySeq;
}


void writeOrigin(W204&w204,QByteArray& data){
// head
	appendBits(w204.var17447653858,2,data,true,true);
	appendBits(w204.var17447664859,2,data,true);
	appendBits(w204.var17447669860,5,data,true);
	appendBits(w204.var17447677861,3,data,true);

// body
 
	int index =0; 
	 int count = 0;	appendBits(w204.var17447134782,3,data,true);
	appendBits(w204.var17447142783,15,data,true);
	appendBits(w204.var17447142784,2,data,true);
	appendBits(w204.var17447151785,3,data,true);
	appendBits(w204.var17447159786,3,data,true);
	appendBits(w204.var17447163787,3,data,true);
	appendBits(w204.var17447166788,5,data,true);
	appendBits(w204.var17447174789,6,data,true);
	appendBits(w204.var17447174790,6,data,true);
	appendBits(w204.var17447185791,14,data,true);

}

void writeProlong(W204&w204,QByteArray& data){
// head
	appendBits(w204.var17447677862,2,data,true);
	appendBits(w204.var17447685863,2,data,true);

// body
 
	int index =0; 
	 int count = 0;	appendBits(w204.var17447191792,0,data,true);
	appendBits(w204.var17447191793,1,data,true);
	appendBits(w204.var17447201794,16,data,true);
	appendBits(w204.var17447207795,26,data,true);
	appendBits(w204.var17447213796,25,data,true);

}

void writeContinue1(W204&w204,QByteArray& data){
// head
	appendBits(w204.var17447694864,2,data,true);
	appendBits(w204.var17447727870,2,data,true);
	appendBits(w204.var17447760876,5,data,true);

// body
 
	int index =0; 
	 int count = 0;	appendBits(w204.var17447216797,0,data,true);
	appendBits(w204.var17447226798,14,data,true);
	appendBits(w204.var17447232799,14,data,true);
	appendBits(w204.var17447240800,14,data,true);
	if( 1 == w204.var17447191793){
	appendBits(w204.var17447248801,5,data,true);
	}

	if( 0 == w204.var17447191793){
	appendBits(w204.var17447248802,5,data,true);
	}

	appendBits(w204.var17447264803,5,data,true);
	appendBits(w204.var17447267804,4,data,true);
	appendBits(w204.var17447281805,3,data,true);
	appendBits(w204.var17447284806,2,data,true);
	appendBits(w204.var17447296807,2,data,true);

}

void writeContinue2(W204&w204,QByteArray& data){
// head
	appendBits(w204.var17447694865,2,data,true);
	appendBits(w204.var17447727871,2,data,true);
	appendBits(w204.var17447760877,5,data,true);

// body
 
	int index =0; 
	 int count = 0;	appendBits(w204.var17447304808,0,data,true);
	appendBits(w204.var17447315809,22,data,true);
	appendBits(w204.var17447320810,19,data,true);
	appendBits(w204.var17447324811,18,data,true);
	appendBits(w204.var17447304808,4,data,true);

}

void writeContinue3(W204&w204,QByteArray& data){
// head
	appendBits(w204.var17447694866,2,data,true);
	appendBits(w204.var17447727872,2,data,true);
	appendBits(w204.var17447760878,5,data,true);

// body
 
	int index =0; 
	 int count = 0;	appendBits(w204.var17447330813,0,data,true);
	appendBits(w204.var17447330814,18,data,true);
	appendBits(w204.var17447340815,18,data,true);
	appendBits(w204.var17447346816,18,data,true);
	appendBits(w204.var17447353817,3,data,true);
	appendBits(w204.var17447361818,3,data,true);
	appendBits(w204.var17447361819,3,data,true);

}

void writeContinue4(W204&w204,QByteArray& data){
// head
	appendBits(w204.var17447694867,2,data,true);
	appendBits(w204.var17447727873,2,data,true);
	appendBits(w204.var17447760879,5,data,true);

// body
 
	int index =0; 
	 int count = 0;	appendBits(w204.var17447377820,0,data,true);
	appendBits(w204.var17447379821,1,data,true);
	if( 1 == w204.var17447281805){
	appendBits(w204.var17447385822,8,data,true);
	appendBits(w204.var17447377820,4,data,true);
	}

	if( 2 == w204.var17447281805){
	appendBits(w204.var17447396824,12,data,true);
	}

	appendBits(w204.var17447402825,6,data,true);
	appendBits(w204.var17447413826,6,data,true);
	appendBits(w204.var17447377820,4,data,true);
	appendBits(w204.var17447419828,2,data,true);
	appendBits(w204.var17447429829,7,data,true);
	appendBits(w204.var17447434830,7,data,true);
	if( 0 == w204.var17447379821){
	appendBits(w204.var17447443831,7,data,true);
	}

	if( 1 == w204.var17447379821){
	appendBits(w204.var17447451832,7,data,true);
	}

	appendBits(w204.var17447459833,4,data,true);
	appendBits(w204.var17447459834,3,data,true);
	appendBits(w204.var17447467835,2,data,true);
	appendBits(w204.var17447475836,2,data,true);

}

void writeContinue5(W204&w204,QByteArray& data){
// head
	appendBits(w204.var17447694868,2,data,true);
	appendBits(w204.var17447727874,2,data,true);
	appendBits(w204.var17447760880,5,data,true);

// body
 
	int index =0; 
	 int count = 0;	appendBits(w204.var17447483837,0,data,true);
	appendBits(w204.var202509201216441,1,data,true);
	appendBits(w204.var17447499839,6,data,true);
	appendBits(w204.var17447507840,6,data,true);
	appendBits(w204.var17447515841,6,data,true);
	appendBits(w204.var17447523842,6,data,true);
	appendBits(w204.var17447531843,6,data,true);
	appendBits(w204.var17447540844,8,data,true);
	appendBits(w204.var17447548845,8,data,true);
	appendBits(w204.var17447548846,8,data,true);
	appendBits(w204.var17447564847,8,data,true);

}

void writeContinue6(W204&w204,QByteArray& data){
// head
	appendBits(w204.var17447694869,2,data,true);
	appendBits(w204.var17447727875,2,data,true);
	appendBits(w204.var17447760881,5,data,true);

// body
 
	int index =0; 
	 int count = 0;	appendBits(w204.var17447567848,0,data,true);
	appendBits(w204.var17447579849,3,data,true);
	appendBits(w204.var17447580850,5,data,true);
	appendBits(w204.var17447595851,14,data,true);
	appendBits(w204.var17447604852,6,data,true);
	appendBits(w204.var17447612853,6,data,true);
	appendBits(w204.var17447620854,6,data,true);
	appendBits(w204.var17447628855,6,data,true);
	appendBits(w204.var17447641856,6,data,true);
	appendBits(w204.var17447649857,11,data,true);

}
QString checkEncodeSeqNumber(W204 &w204)
{
	QString seqNum;
	int flag =0;
 
	int index =0; 
	 int count = 0;
	flag = 0;
	flag+=w204.var17447134782!=0;
	flag+=w204.var17447142783!=0;
	flag+=w204.var17447142784!=0;
	flag+=w204.var17447151785!=0;
	flag+=w204.var17447159786!=0;
	flag+=w204.var17447163787!=0;
	flag+=w204.var17447166788!=0;
	flag+=w204.var17447174789!=0;
	flag+=w204.var17447174790!=0;
	flag+=w204.var17447185791!=0;
	if(flag != 0){seqNum+="3";}
	flag = 0;
	flag+=w204.var17447191792!=0;
	flag+=w204.var17447191793!=1;
	flag+=w204.var17447201794!=0;
	flag+=w204.var17447207795!=0;
	flag+=w204.var17447213796!=0;
	if(flag != 0){seqNum+="11";}
	flag = 0;
	flag+=w204.var17447216797!=0;
	flag+=w204.var17447226798!=0;
	flag+=w204.var17447232799!=0;
	flag+=w204.var17447240800!=0;
	if( 1 == w204.var17447191793){

	flag+=w204.var17447248801!=0;
	}


	if( 0 == w204.var17447191793){

	flag+=w204.var17447248802!=0;
	}


	flag+=w204.var17447264803!=0;
	flag+=w204.var17447267804!=0;
	flag+=w204.var17447281805!=1;
	flag+=w204.var17447284806!=0;
	flag+=w204.var17447296807!=0;
	if(flag != 0){seqNum+="21";}
	flag = 0;
	flag+=w204.var17447304808!=0;
	flag+=w204.var17447315809!=0;
	flag+=w204.var17447320810!=0;
	flag+=w204.var17447324811!=0;
	flag+=w204.var17447304808!=0;
	if(flag != 0){seqNum+="22";}
	flag = 0;
	flag+=w204.var17447330813!=0;
	flag+=w204.var17447330814!=0;
	flag+=w204.var17447340815!=0;
	flag+=w204.var17447346816!=0;
	flag+=w204.var17447353817!=0;
	flag+=w204.var17447361818!=0;
	flag+=w204.var17447361819!=0;
	if(flag != 0){seqNum+="23";}
	flag = 0;
	flag+=w204.var17447377820!=0;
	flag+=w204.var17447379821!=0;
	if( 1 == w204.var17447281805){

	flag+=w204.var17447385822!=0;
	flag+=w204.var17447377820!=0;
	}


	if( 2 == w204.var17447281805){

	flag+=w204.var17447396824!=0;
	}


	flag+=w204.var17447402825!=0;
	flag+=w204.var17447413826!=0;
	flag+=w204.var17447377820!=0;
	flag+=w204.var17447419828!=0;
	flag+=w204.var17447429829!=0;
	flag+=w204.var17447434830!=0;
	if( 0 == w204.var17447379821){

	flag+=w204.var17447443831!=0;
	}


	if( 1 == w204.var17447379821){

	flag+=w204.var17447451832!=0;
	}


	flag+=w204.var17447459833!=0;
	flag+=w204.var17447459834!=0;
	flag+=w204.var17447467835!=0;
	flag+=w204.var17447475836!=0;
	if(flag != 0){seqNum+="24";}
	flag = 0;
	flag+=w204.var17447483837!=0;
	flag+=w204.var202509201216441!=0;
	flag+=w204.var17447499839!=0;
	flag+=w204.var17447507840!=0;
	flag+=w204.var17447515841!=0;
	flag+=w204.var17447523842!=0;
	flag+=w204.var17447531843!=0;
	flag+=w204.var17447540844!=0;
	flag+=w204.var17447548845!=0;
	flag+=w204.var17447548846!=0;
	flag+=w204.var17447564847!=0;
	if(flag != 0){seqNum+="25";}
	flag = 0;
	flag+=w204.var17447567848!=0;
	flag+=w204.var17447579849!=0;
	flag+=w204.var17447580850!=0;
	flag+=w204.var17447595851!=0;
	flag+=w204.var17447604852!=0;
	flag+=w204.var17447612853!=0;
	flag+=w204.var17447620854!=0;
	flag+=w204.var17447628855!=0;
	flag+=w204.var17447641856!=0;
	flag+=w204.var17447649857!=0;
	if(flag != 0){seqNum+="26";}
	 return seqNum;}

void VerifyField(W204 &w204)
{
	int flag = 0; 

	 flag = 0;
	 flag += (0 != w204.var17447248801);
	if( 0 != flag )
		w204.var17447191793=1;

	 flag = 0;
	 flag += (0 != w204.var17447248802);
	if( 0 != flag )
		w204.var17447191793=0;

	 flag = 0;
	 flag += (0 != w204.var17447385822);
	 flag += (0 != w204.var17447377820);
	if( 0 != flag )
		w204.var17447281805=1;

	 flag = 0;
	 flag += (0 != w204.var17447396824);
	if( 0 != flag )
		w204.var17447281805=2;

	 flag = 0;
	 flag += (0 != w204.var17447443831);
	if( 0 != flag )
		w204.var17447379821=0;

	 flag = 0;
	 flag += (0 != w204.var17447451832);
	if( 0 != flag )
		w204.var17447379821=1;
}
void updateFieldValue(W204 &w204)
{

	//bp_备用位数为0,数据放到头hp_**字段中
	 w204.var17447677862=w204.var17447191792;

	//bc1_备用位数为0,数据放到头hc1_**字段中
	 w204.var17447694864=w204.var17447216797;

	//bc2_备用位数为0,数据放到头hc2_**字段中
	 w204.var17447694865=w204.var17447304808;

	//bc3_备用位数为0,数据放到头hc3_**字段中
	 w204.var17447694866=w204.var17447330813;

	//bc4_备用位数为0,数据放到头hc4_**字段中
	 w204.var17447694867=w204.var17447377820;

	//bc5_备用位数为0,数据放到头hc5_**字段中
	 w204.var17447694868=w204.var17447483837;

	//bc6_备用位数为0,数据放到头hc6_**字段中
	 w204.var17447694869=w204.var17447567848;
}

void updateGroupFlag(W204 &w204)
{
}
void encodeMsg(QByteArray& data, W204 &w204){

	QString strSeqNum=checkEncodeSeqNumber(w204);
	QString temSeqNum;
	 temSeqNum="311212223242526";
	if(temSeqNum.contains(strSeqNum)){
	//Seq_1
		setVerify_1(w204,data);  
		return;
	}
	writeSeq_1(w204,data);
}


void readOrigin(S106&s106,uchar * & pMsgData, int & msgLens,int & pos){
// body
	checkPosition(pMsgData, msgLens,pos,15);
	s106.ijsptbzhj = readBits(pMsgData, msgLens, pos ,15);
	pos += 15;

	checkPosition(pMsgData, msgLens,pos,5);
	s106.b0zt = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

	checkPosition(pMsgData, msgLens,pos,1);
	s106.boyxbs = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,3);
	s106.bomusx = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	checkPosition(pMsgData, msgLens,pos,1);
	s106.bosyhly = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,1);
	s106.boby = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,6);
	s106.bosyh = readBits(pMsgData, msgLens, pos ,6);
	pos += 6;

	checkPosition(pMsgData, msgLens,pos,1);
	s106.boias = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;
	if( 0 == s106.boias){

	checkPosition(pMsgData, msgLens,pos,13);
	s106.jwh.idd = readBits(pMsgData, msgLens, pos ,13);
	pos += 13;
	}

	if( 1 == s106.boias){

	checkPosition(pMsgData, msgLens,pos,8);
	s106.bosd = readBits(pMsgData, msgLens, pos ,8);
	pos += 8;

	checkPosition(pMsgData, msgLens,pos,5);
	s106.boass = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;
	}


	checkPosition(pMsgData, msgLens,pos,1);
	s106.boshjd = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,10);
	s106.boxdsj = readBits(pMsgData, msgLens, pos ,10);
	pos += 10;

}

void readProlong(S106&s106,uchar * & pMsgData, int & msgLens,int & pos){
// head

	checkPosition(pMsgData, msgLens,pos,2);
	s106.hpby2 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	s106.hpzbs = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

// body
	checkPosition(pMsgData, msgLens,pos,1);
	s106.bpby = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,12);
	s106.ihx = readBits(pMsgData, msgLens, pos ,12);
	pos += 12;

	checkPosition(pMsgData, msgLens,pos,11);
	s106.isdu = readBits(pMsgData, msgLens, pos ,11);
	pos += 11;

	checkPosition(pMsgData, msgLens,pos,1);
	s106.bpdlzb = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;
	if( 0 == s106.bpdlzb){

	checkPosition(pMsgData, msgLens,pos,22);
	s106.jwh.ijd = readBits(pMsgData, msgLens, pos ,22);
	pos += 22;

	checkPosition(pMsgData, msgLens,pos,21);
	s106.jwh.iwd = readBits(pMsgData, msgLens, pos ,21);
	pos += 21;
	}

	if( 1 == s106.bpdlzb){

	checkPosition(pMsgData, msgLens,pos,2);
	s106.bpwzlg = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,1);
	s106.bp87 = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,16);
	s106.ibpx = readBits(pMsgData, msgLens, pos ,16);
	pos += 16;

	checkPosition(pMsgData, msgLens,pos,1);
	s106.bpyfhw = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,16);
	s106.ibpy = readBits(pMsgData, msgLens, pos ,16);
	pos += 16;

	checkPosition(pMsgData, msgLens,pos,7);
	s106.ibpbyfl = readBits(pMsgData, msgLens, pos ,7);
	pos += 7;
	}


}

void readContinue1(S106&s106,uchar * & pMsgData, int & msgLens,int & pos){
// head

	checkPosition(pMsgData, msgLens,pos,2);
	s106.hc1by = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	s106.hc1zbs = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,5);
	s106.hc1jxzbs = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

// body
	checkPosition(pMsgData, msgLens,pos,4);
	s106.bphjzl = readBits(pMsgData, msgLens, pos ,4);
	pos += 4;

	checkPosition(pMsgData, msgLens,pos,4);
	s106.imbsl = readBits(pMsgData, msgLens, pos ,4);
	pos += 4;

	checkPosition(pMsgData, msgLens,pos,3);
	s106.ihjlb = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	checkPosition(pMsgData, msgLens,pos,1);
	s106.ibc1mylx = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;
	if( 0 == s106.ibc1mylx){

	checkPosition(pMsgData, msgLens,pos,8);
	s106.bc1mblx = readBits(pMsgData, msgLens, pos ,8);
	pos += 8;

	checkPosition(pMsgData, msgLens,pos,4);
	s106.ibc1by = readBits(pMsgData, msgLens, pos ,4);
	pos += 4;
	}

	if( 1 == s106.ibc1mylx){

	checkPosition(pMsgData, msgLens,pos,12);
	s106.bc1mbxh = readBits(pMsgData, msgLens, pos ,12);
	pos += 12;
	}


	checkPosition(pMsgData, msgLens,pos,18);
	s106.bc1by = readBits(pMsgData, msgLens, pos ,18);
	pos += 18;

	checkPosition(pMsgData, msgLens,pos,19);
	s106.bc1mbbsh = readBits(pMsgData, msgLens, pos ,19);
	pos += 19;

	checkPosition(pMsgData, msgLens,pos,1);
	s106.bc1bzsh = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,1);
	s106.bc1zsf = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

}

void readContinue2(S106&s106,uchar * & pMsgData, int & msgLens,int & pos){
// head

	checkPosition(pMsgData, msgLens,pos,2);
	s106.hc2by = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	s106.hc2zbs = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,5);
	s106.hc2jxzbs = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

// body
	checkPosition(pMsgData, msgLens,pos,5);
	s106.bc2wfwqxt = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

	checkPosition(pMsgData, msgLens,pos,5);
	s106.bc2wqjzzt = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

	checkPosition(pMsgData, msgLens,pos,19);
	s106.bc2wmbh = readBits(pMsgData, msgLens, pos ,19);
	pos += 19;

	checkPosition(pMsgData, msgLens,pos,15);
	s106.bc2sysfzbh = readBits(pMsgData, msgLens, pos ,15);
	pos += 15;

	checkPosition(pMsgData, msgLens,pos,1);
	s106.bc2by = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,6);
	s106.bc2wsfsyh = readBits(pMsgData, msgLens, pos ,6);
	pos += 6;

	checkPosition(pMsgData, msgLens,pos,12);
	s106.bc2by = readBits(pMsgData, msgLens, pos ,12);
	pos += 12;

}

void readContinue3(S106&s106,uchar * & pMsgData, int & msgLens,int & pos){
// head

	checkPosition(pMsgData, msgLens,pos,2);
	s106.hc3by = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	s106.hc3zbs = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,5);
	s106.hc3jxzbsf = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

// body
	checkPosition(pMsgData, msgLens,pos,4);
	s106.bc2cgqlx = readBits(pMsgData, msgLens, pos ,4);
	pos += 4;

	checkPosition(pMsgData, msgLens,pos,3);
	s106.bc3grjlx = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	checkPosition(pMsgData, msgLens,pos,20);
	s106.bc3by = readBits(pMsgData, msgLens, pos ,20);
	pos += 20;

	checkPosition(pMsgData, msgLens,pos,1);
	s106.bc3rhlx = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,1);
	s106.bc3mbbgjs = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,3);
	s106.bc3fsyprf = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	checkPosition(pMsgData, msgLens,pos,4);
	s106.bc3fsyzt = readBits(pMsgData, msgLens, pos ,4);
	pos += 4;

	checkPosition(pMsgData, msgLens,pos,3);
	s106.bc3sjgn = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	checkPosition(pMsgData, msgLens,pos,5);
	s106.bc3sj = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

	checkPosition(pMsgData, msgLens,pos,6);
	s106.bc3fz = readBits(pMsgData, msgLens, pos ,6);
	pos += 6;

	checkPosition(pMsgData, msgLens,pos,6);
	s106.bc3m = readBits(pMsgData, msgLens, pos ,6);
	pos += 6;

	checkPosition(pMsgData, msgLens,pos,7);
	s106.bc3shm = readBits(pMsgData, msgLens, pos ,7);
	pos += 7;

}

void readContinue4(S106&s106,uchar * & pMsgData, int & msgLens,int & pos){
// head

	checkPosition(pMsgData, msgLens,pos,2);
	s106.hc4by = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	s106.hc4zbs = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,5);
	s106.hc4jxzbsf = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

// body
	checkPosition(pMsgData, msgLens,pos,4);
	s106.bc4by = readBits(pMsgData, msgLens, pos ,4);
	pos += 4;

	checkPosition(pMsgData, msgLens,pos,4);
	s106.bc4xjlwzbmq = readBits(pMsgData, msgLens, pos ,4);
	pos += 4;

	checkPosition(pMsgData, msgLens,pos,4);
	s106.bc4cpwzbm = readBits(pMsgData, msgLens, pos ,4);
	pos += 4;

	checkPosition(pMsgData, msgLens,pos,4);
	s106.bc4zzwzbmq = readBits(pMsgData, msgLens, pos ,4);
	pos += 4;

	checkPosition(pMsgData, msgLens,pos,11);
	s106.bc4fwjbqd = readBits(pMsgData, msgLens, pos ,11);
	pos += 11;

	checkPosition(pMsgData, msgLens,pos,11);
	s106.bc4qjbqd = readBits(pMsgData, msgLens, pos ,11);
	pos += 11;

	checkPosition(pMsgData, msgLens,pos,13);
	s106.bc4sdx = readBits(pMsgData, msgLens, pos ,13);
	pos += 13;

	checkPosition(pMsgData, msgLens,pos,4);
	s106.bc4sdbbqdd = readBits(pMsgData, msgLens, pos ,4);
	pos += 4;

	checkPosition(pMsgData, msgLens,pos,4);
	s106.bc4sddbqd = readBits(pMsgData, msgLens, pos ,4);
	pos += 4;

	checkPosition(pMsgData, msgLens,pos,4);
	s106.bc4sdxbqd = readBits(pMsgData, msgLens, pos ,4);
	pos += 4;

}

void readContinue5(S106&s106,uchar * & pMsgData, int & msgLens,int & pos){
// head

	checkPosition(pMsgData, msgLens,pos,2);
	s106.hc5by = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	s106.hc5zbs = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,5);
	s106.hc5jxzbsf = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

// body
	checkPosition(pMsgData, msgLens,pos,3);
	s106.bc5by = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	checkPosition(pMsgData, msgLens,pos,9);
	s106.bc5wfbdgl = readBits(pMsgData, msgLens, pos ,9);
	pos += 9;

	checkPosition(pMsgData, msgLens,pos,9);
	s106.bc5dfbdgl = readBits(pMsgData, msgLens, pos ,9);
	pos += 9;

	checkPosition(pMsgData, msgLens,pos,12);
	s106.bc5mhxh1 = readBits(pMsgData, msgLens, pos ,12);
	pos += 12;

	checkPosition(pMsgData, msgLens,pos,12);
	s106.bc5mhxh2 = readBits(pMsgData, msgLens, pos ,12);
	pos += 12;

	checkPosition(pMsgData, msgLens,pos,9);
	s106.bc5mhxh1gl = readBits(pMsgData, msgLens, pos ,9);
	pos += 9;

	checkPosition(pMsgData, msgLens,pos,9);
	s106.bc5mhxh2gl = readBits(pMsgData, msgLens, pos ,9);
	pos += 9;

}

void readContinue6(S106&s106,uchar * & pMsgData, int & msgLens,int & pos){
// head

	checkPosition(pMsgData, msgLens,pos,2);
	s106.hc6by = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	s106.hc6zbs = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,5);
	s106.bc6jxzbsf = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

// body
	checkPosition(pMsgData, msgLens,pos,12);
	s106.bc6by = readBits(pMsgData, msgLens, pos ,12);
	pos += 12;

	checkPosition(pMsgData, msgLens,pos,3);
	s106.bc6jlsjmsf = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	checkPosition(pMsgData, msgLens,pos,5);
	s106.bc6xs = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

	checkPosition(pMsgData, msgLens,pos,6);
	s106.bc6fzfssk = readBits(pMsgData, msgLens, pos ,6);
	pos += 6;

	checkPosition(pMsgData, msgLens,pos,6);
	s106.bc6m = readBits(pMsgData, msgLens, pos ,6);
	pos += 6;

	checkPosition(pMsgData, msgLens,pos,7);
	s106.bc6hm = readBits(pMsgData, msgLens, pos ,7);
	pos += 7;

	checkPosition(pMsgData, msgLens,pos,5);
	s106.bc6by = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

	checkPosition(pMsgData, msgLens,pos,6);
	s106.bc6fzjxsk = readBits(pMsgData, msgLens, pos ,6);
	pos += 6;

	checkPosition(pMsgData, msgLens,pos,6);
	s106.bc6msjdasd = readBits(pMsgData, msgLens, pos ,6);
	pos += 6;

	checkPosition(pMsgData, msgLens,pos,7);
	s106.bc6hmsjasidsd = readBits(pMsgData, msgLens, pos ,7);
	pos += 7;

}

void readContinue7(S106&s106,uchar * & pMsgData, int & msgLens,int & pos){
// head

	checkPosition(pMsgData, msgLens,pos,2);
	s106.var202408231856031 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	s106.var2024082319035313 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,5);
	s106.var2024082319050515 = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

// body
	checkPosition(pMsgData, msgLens,pos,12);
	s106.var202408231856592 = readBits(pMsgData, msgLens, pos ,12);
	pos += 12;

	checkPosition(pMsgData, msgLens,pos,3);
	s106.var202408231859244 = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	checkPosition(pMsgData, msgLens,pos,5);
	s106.var202408231859475 = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

	checkPosition(pMsgData, msgLens,pos,6);
	s106.var202408231900156 = readBits(pMsgData, msgLens, pos ,6);
	pos += 6;

	checkPosition(pMsgData, msgLens,pos,6);
	s106.var202408231900407 = readBits(pMsgData, msgLens, pos ,6);
	pos += 6;

	checkPosition(pMsgData, msgLens,pos,7);
	s106.var202408231901088 = readBits(pMsgData, msgLens, pos ,7);
	pos += 7;

	checkPosition(pMsgData, msgLens,pos,5);
	s106.var202408231856592 = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

	checkPosition(pMsgData, msgLens,pos,6);
	s106.var2024082319022610 = readBits(pMsgData, msgLens, pos ,6);
	pos += 6;

	checkPosition(pMsgData, msgLens,pos,6);
	s106.var2024082319025311 = readBits(pMsgData, msgLens, pos ,6);
	pos += 6;

	checkPosition(pMsgData, msgLens,pos,7);
	s106.var2024082319032012 = readBits(pMsgData, msgLens, pos ,7);
	pos += 7;

}
QString  checkS106SeqNum(QString strSeq)
{
	
	static QMap<QString,std::shared_ptr<MessCodeInfo>>  proSeqMap; 
	static int index = 0;                     
	if( 0 == index++ )
	{
     
	std::shared_ptr<MessCodeInfo> Seq_1(new MessCodeInfo); 
	Seq_1->icycle=100;
	Seq_1->itimes=3;
	Seq_1->strSeq="311212223242526";
	proSeqMap["Seq_1"] = Seq_1; 

	std::shared_ptr<MessCodeInfo> Seq_2(new MessCodeInfo); 
	Seq_2->icycle=100;
	Seq_2->itimes=3;
	Seq_2->strSeq="31121222324252627";
	proSeqMap["Seq_2"] = Seq_2; 

	}
	QString ret;
	auto ptr = proSeqMap.begin();        
	while (ptr != proSeqMap.end())        
	{            
		if (ptr.value()->strSeq == strSeq)            
		{                
			ret = ptr.key();                
			break;            
		}            
		ptr++;        
	}

	return ret;
        
}
void readOrigin(S106&s106,uchar * & pMsgData, int & msgLens,int & pos);
void readProlong(S106&s106,uchar * & pMsgData, int & msgLens,int & pos);
void readContinue1(S106&s106,uchar * & pMsgData, int & msgLens,int & pos);
void readContinue2(S106&s106,uchar * & pMsgData, int & msgLens,int & pos);
void readContinue3(S106&s106,uchar * & pMsgData, int & msgLens,int & pos);
void readContinue4(S106&s106,uchar * & pMsgData, int & msgLens,int & pos);
void readContinue5(S106&s106,uchar * & pMsgData, int & msgLens,int & pos);
void readContinue6(S106&s106,uchar * & pMsgData, int & msgLens,int & pos);
void readContinue7(S106&s106,uchar * & pMsgData, int & msgLens,int & pos);
void readS106(int key,S106 &s106,uchar * & pMsgData, int & msgLens,int & pos)
{
	switch (key)
{
	case 3:readOrigin(s106,pMsgData,msgLens,pos);break; 
	case 11:readProlong(s106,pMsgData,msgLens,pos);break;
	case 21:readContinue1(s106,pMsgData,msgLens,pos);break;
	case 22:readContinue2(s106,pMsgData,msgLens,pos);break;
	case 23:readContinue3(s106,pMsgData,msgLens,pos);break;
	case 24:readContinue4(s106,pMsgData,msgLens,pos);break;
	case 25:readContinue5(s106,pMsgData,msgLens,pos);break;
	case 26:readContinue6(s106,pMsgData,msgLens,pos);break;
	case 27:readContinue7(s106,pMsgData,msgLens,pos);break;
	default:
		break;
	}
}

void writeContinue4(S106&s106,QByteArray& data);

void writeContinue2(S106&s106,QByteArray& data);

void writeContinue5(S106&s106,QByteArray& data);

void writeProlong(S106&s106,QByteArray& data);

void writeOrigin(S106&s106,QByteArray& data);

void writeContinue1(S106&s106,QByteArray& data);
void updateFieldValue(S106 &s106);
void VerifyField(S106 &s106);

void writeContinue3(S106&s106,QByteArray& data);

void writeContinue7(S106&s106,QByteArray& data);

void writeContinue6(S106&s106,QByteArray& data);
void updateGroupFlag(S106 &s106);
static void writeSeq_1(S106&s106,QByteArray& data)
{

	VerifyField(s106);
	updateFieldValue(s106);
	updateGroupFlag(s106);
	writeOrigin(s106,data);
	writeProlong(s106,data);
	writeContinue1(s106,data);
	writeContinue2(s106,data);
	writeContinue3(s106,data);
	writeContinue4(s106,data);
	writeContinue5(s106,data);
	writeContinue6(s106,data);
}
static void writeSeq_2(S106&s106,QByteArray& data)
{

	VerifyField(s106);
	updateFieldValue(s106);
	updateGroupFlag(s106);
	writeOrigin(s106,data);
	writeProlong(s106,data);
	writeContinue1(s106,data);
	writeContinue2(s106,data);
	writeContinue3(s106,data);
	writeContinue4(s106,data);
	writeContinue5(s106,data);
	writeContinue6(s106,data);
	writeContinue7(s106,data);
}

static bool checkatom1(S106 &s106)
{// 校验S106_Origin:Item.状态/命令值值
	return s106.b0zt==1;

}

static bool setatom1(S106 &s106)
{// 设置S106_Origin:Item.状态/命令值值
	return s106.b0zt=1;

}

static bool checkatom2(S106 &s106)
{// 校验S106_Origin:Item.状态/命令值值
	return s106.b0zt==2;

}

static bool setatom2(S106 &s106)
{// 设置S106_Origin:Item.状态/命令值值
	return s106.b0zt=2;

}

static bool checkatom3(S106 &s106)
{// 校验S106_Origin:Item.时间精度标识值
	return s106.boshjd==1;

}

static bool setatom3(S106 &s106)
{// 设置S106_Origin:Item.时间精度标识值
	return s106.boshjd=1;

}

static bool checkatom4(S106 &s106)
{// 校验S106_Origin:Item.时间精度标识值
	return s106.boshjd==0;

}

static bool setatom4(S106 &s106)
{// 设置S106_Origin:Item.时间精度标识值
	return s106.boshjd=0;

}

static bool checkConstraint1(S106 &s106)
{// 计算 Constraint1值
	return checkatom1(s106)&checkatom3(s106);

}

static bool setConstraint1(S106 &s106)
{// 设置 Constraint1值
	return setatom1(s106)&setatom3(s106);

}

static bool checkConstraint2(S106 &s106)
{// 计算 Constraint2值
	return checkatom2(s106)&checkatom4(s106);

}

static bool setConstraint2(S106 &s106)
{// 设置 Constraint2值
	return setatom2(s106)&setatom4(s106);

}

static QString checkverify1(S106 &s106,QString seq)
{
	return  (seq=="Seq_1"&&checkConstraint1(s106) )?"verify1":"" ;

}

static bool setverify1(S106 &s106,QByteArray& data)
{
	  setConstraint1(s106)  ;

	writeSeq_1(s106,data);
	return true;
}

static QString checkverify2(S106 &s106,QString seq)
{
	return  (seq=="Seq_2"&&checkConstraint2(s106) )?"verify2":"" ;

}

static bool setverify2(S106 &s106,QByteArray& data)
{
	  setConstraint2(s106)  ;

	writeSeq_2(s106,data);
	return true;
}

int checkObjMaps(QString strVerify,QByteArray& data, S106 &s106)
{

	if(strVerify== "verify2")
	{
		setverify1(s106,data);
		return 0;
	 } 
	if(strVerify== "verify1")
	{
        return 0;
	 } 
	return -1;
}
static QString  Verifys106Seq(S106&s106,QString seq)
{
	 for(int i=0;i < 2;i++)
	{
		switch (i)
		{
		case 0:                   
			{                   
				QString str = checkverify1(s106,seq);                   
				if(  str.isEmpty()==false)                    
					return str;
			}break;
		case 1:                   
			{                   
				QString str = checkverify2(s106,seq);                   
				if(  str.isEmpty()==false)                    
					return str;
			}break;
		}
	}
	return "";
}
QString decodeMsg( uchar * pData, int len, S106 &s106){
	 int pos = 0;
	 unsigned char *pMsgData = pData;
	 int msgLens = len;
	 int index = 1;

// head
	checkPosition(pMsgData, msgLens,pos,2);
	s106.b0byqq = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	s106.bozbs = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,5);
	s106.hoxxbs = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

	checkPosition(pMsgData, msgLens,pos,3);
	s106.boxxzbs = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	checkPosition(pMsgData, msgLens,pos,3);
	s106.boxxcd = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	readOrigin(s106, pMsgData, msgLens, pos); 

	int ip= pos%8 == 0 ? 0:(8 - pos%8);
	pos+=ip;
		checkPosition(pMsgData, msgLens, pos, ip); 
	QStringList seqNum;
	seqNum.append("3");
	while( msgLens) 
	{                
		int temPos = pos;                
		unsigned char* pMsg = pMsgData;                
		int temLen = msgLens;                
		int by = 0;                
		int wordFlag = 0;                
		int wontinueWord = 0;
		by=readBits(pMsg, temLen, temPos ,2);

		temPos+=2;

		checkPosition(pMsg, temLen, temPos, 2); 
		wordFlag=readBits(pMsg, temLen, temPos ,2);

		temPos+=2;

		if(1==wordFlag)
		{ 
			 int key = wordFlag*10+index++; 
			 if (seqNum.indexOf(QString::number(key)) == -1)
				seqNum.append(QString::number(key));
			readS106(key,s106, pMsgData, msgLens, pos); 
			pos += pos%8 == 0 ? 0:(8 - pos%8);
			if(temLen==msgLens) break;
		 }
		else if(2==wordFlag)
		{ 
			wontinueWord=readBits(pMsg, temLen, temPos ,5);
 
			 int key = wordFlag*10+wontinueWord;
			  if (seqNum.indexOf(QString::number(key)) == -1)
				seqNum.append(QString::number(key));
			readS106(key,s106, pMsgData, msgLens, pos);
			pos += pos%8 == 0 ? 0:(8 - pos%8);
			if(temLen==msgLens) break; 
		 }
		else{
			 qDebug()<< __func__ <<" "<<__LINE__<<" msgLen:"<<msgLens <<"pos:"<<pos; break; }
		temPos = (pos%8 != 0)?pos/8 +1:pos/8;
		if((msgLens-temPos) == 0){break;}
	}
	QString strSeq,strSeqNum=seqNum.join("");                         
	 strSeq =checkS106SeqNum( strSeqNum);
	 if(strSeq.isEmpty()==true) return "";
	 QString verifySeq = Verifys106Seq(s106, strSeq);
	 qDebug()<<"recv SeqNum:"<<  strSeqNum <<" recv Seq: " << strSeq << " recv verifySeq: " << verifySeq;  
	 if(verifySeq.isEmpty()==true)
	{
		return "";	}
	 return verifySeq;
}


void writeOrigin(S106&s106,QByteArray& data){
// head
	appendBits(s106.b0byqq,2,data,true,true);
	appendBits(s106.bozbs,2,data,true);
	appendBits(s106.hoxxbs,5,data,true);
	appendBits(s106.boxxzbs,3,data,true);
	appendBits(s106.boxxcd,3,data,true);

// body
 
	int index =0; 
	 int count = 0;	appendBits(s106.ijsptbzhj,15,data,true);
	appendBits(s106.b0zt,5,data,true);
	appendBits(s106.boyxbs,1,data,true);
	appendBits(s106.bomusx,3,data,true);
	appendBits(s106.bosyhly,1,data,true);
	appendBits(s106.boby,1,data,true);
	appendBits(s106.bosyh,6,data,true);
	appendBits(s106.boias,1,data,true);
	if( 0 == s106.boias){
	appendBits(s106.jwh.idd,13,data,true);
	}

	if( 1 == s106.boias){
	appendBits(s106.bosd,8,data,true);
	appendBits(s106.boass,5,data,true);
	}

	appendBits(s106.boshjd,1,data,true);
	appendBits(s106.boxdsj,10,data,true);

}

void writeProlong(S106&s106,QByteArray& data){
// head
	appendBits(s106.hpby2,2,data,true);
	appendBits(s106.hpzbs,2,data,true);

// body
 
	int index =0; 
	 int count = 0;	appendBits(s106.bpby,1,data,true);
	appendBits(s106.ihx,12,data,true);
	appendBits(s106.isdu,11,data,true);
	appendBits(s106.bpdlzb,1,data,true);
	if( 0 == s106.bpdlzb){
	appendBits(s106.jwh.ijd,22,data,true);
	appendBits(s106.jwh.iwd,21,data,true);
	}

	if( 1 == s106.bpdlzb){
	appendBits(s106.bpwzlg,2,data,true);
	appendBits(s106.bp87,1,data,true);
	appendBits(s106.ibpx,16,data,true);
	appendBits(s106.bpyfhw,1,data,true);
	appendBits(s106.ibpy,16,data,true);
	appendBits(s106.ibpbyfl,7,data,true);
	}


}

void writeContinue1(S106&s106,QByteArray& data){
// head
	appendBits(s106.hc1by,2,data,true);
	appendBits(s106.hc1zbs,2,data,true);
	appendBits(s106.hc1jxzbs,5,data,true);

// body
 
	int index =0; 
	 int count = 0;	appendBits(s106.bphjzl,4,data,true);
	appendBits(s106.imbsl,4,data,true);
	appendBits(s106.ihjlb,3,data,true);
	appendBits(s106.ibc1mylx,1,data,true);
	if( 0 == s106.ibc1mylx){
	appendBits(s106.bc1mblx,8,data,true);
	appendBits(s106.ibc1by,4,data,true);
	}

	if( 1 == s106.ibc1mylx){
	appendBits(s106.bc1mbxh,12,data,true);
	}

	appendBits(s106.bc1by,18,data,true);
	appendBits(s106.bc1mbbsh,19,data,true);
	appendBits(s106.bc1bzsh,1,data,true);
	appendBits(s106.bc1zsf,1,data,true);

}

void writeContinue2(S106&s106,QByteArray& data){
// head
	appendBits(s106.hc2by,2,data,true);
	appendBits(s106.hc2zbs,2,data,true);
	appendBits(s106.hc2jxzbs,5,data,true);

// body
 
	int index =0; 
	 int count = 0;	appendBits(s106.bc2wfwqxt,5,data,true);
	appendBits(s106.bc2wqjzzt,5,data,true);
	appendBits(s106.bc2wmbh,19,data,true);
	appendBits(s106.bc2sysfzbh,15,data,true);
	appendBits(s106.bc2by,1,data,true);
	appendBits(s106.bc2wsfsyh,6,data,true);
	appendBits(s106.bc2by,12,data,true);

}

void writeContinue3(S106&s106,QByteArray& data){
// head
	appendBits(s106.hc3by,2,data,true);
	appendBits(s106.hc3zbs,2,data,true);
	appendBits(s106.hc3jxzbsf,5,data,true);

// body
 
	int index =0; 
	 int count = 0;	appendBits(s106.bc2cgqlx,4,data,true);
	appendBits(s106.bc3grjlx,3,data,true);
	appendBits(s106.bc3by,20,data,true);
	appendBits(s106.bc3rhlx,1,data,true);
	appendBits(s106.bc3mbbgjs,1,data,true);
	appendBits(s106.bc3fsyprf,3,data,true);
	appendBits(s106.bc3fsyzt,4,data,true);
	appendBits(s106.bc3sjgn,3,data,true);
	appendBits(s106.bc3sj,5,data,true);
	appendBits(s106.bc3fz,6,data,true);
	appendBits(s106.bc3m,6,data,true);
	appendBits(s106.bc3shm,7,data,true);

}

void writeContinue4(S106&s106,QByteArray& data){
// head
	appendBits(s106.hc4by,2,data,true);
	appendBits(s106.hc4zbs,2,data,true);
	appendBits(s106.hc4jxzbsf,5,data,true);

// body
 
	int index =0; 
	 int count = 0;	appendBits(s106.bc4by,4,data,true);
	appendBits(s106.bc4xjlwzbmq,4,data,true);
	appendBits(s106.bc4cpwzbm,4,data,true);
	appendBits(s106.bc4zzwzbmq,4,data,true);
	appendBits(s106.bc4fwjbqd,11,data,true);
	appendBits(s106.bc4qjbqd,11,data,true);
	appendBits(s106.bc4sdx,13,data,true);
	appendBits(s106.bc4sdbbqdd,4,data,true);
	appendBits(s106.bc4sddbqd,4,data,true);
	appendBits(s106.bc4sdxbqd,4,data,true);

}

void writeContinue5(S106&s106,QByteArray& data){
// head
	appendBits(s106.hc5by,2,data,true);
	appendBits(s106.hc5zbs,2,data,true);
	appendBits(s106.hc5jxzbsf,5,data,true);

// body
 
	int index =0; 
	 int count = 0;	appendBits(s106.bc5by,3,data,true);
	appendBits(s106.bc5wfbdgl,9,data,true);
	appendBits(s106.bc5dfbdgl,9,data,true);
	appendBits(s106.bc5mhxh1,12,data,true);
	appendBits(s106.bc5mhxh2,12,data,true);
	appendBits(s106.bc5mhxh1gl,9,data,true);
	appendBits(s106.bc5mhxh2gl,9,data,true);

}

void writeContinue6(S106&s106,QByteArray& data){
// head
	appendBits(s106.hc6by,2,data,true);
	appendBits(s106.hc6zbs,2,data,true);
	appendBits(s106.bc6jxzbsf,5,data,true);

// body
 
	int index =0; 
	 int count = 0;	appendBits(s106.bc6by,12,data,true);
	appendBits(s106.bc6jlsjmsf,3,data,true);
	appendBits(s106.bc6xs,5,data,true);
	appendBits(s106.bc6fzfssk,6,data,true);
	appendBits(s106.bc6m,6,data,true);
	appendBits(s106.bc6hm,7,data,true);
	appendBits(s106.bc6by,5,data,true);
	appendBits(s106.bc6fzjxsk,6,data,true);
	appendBits(s106.bc6msjdasd,6,data,true);
	appendBits(s106.bc6hmsjasidsd,7,data,true);

}

void writeContinue7(S106&s106,QByteArray& data){
// head
	appendBits(s106.var202408231856031,2,data,true);
	appendBits(s106.var2024082319035313,2,data,true);
	appendBits(s106.var2024082319050515,5,data,true);

// body
 
	int index =0; 
	 int count = 0;	appendBits(s106.var202408231856592,12,data,true);
	appendBits(s106.var202408231859244,3,data,true);
	appendBits(s106.var202408231859475,5,data,true);
	appendBits(s106.var202408231900156,6,data,true);
	appendBits(s106.var202408231900407,6,data,true);
	appendBits(s106.var202408231901088,7,data,true);
	appendBits(s106.var202408231856592,5,data,true);
	appendBits(s106.var2024082319022610,6,data,true);
	appendBits(s106.var2024082319025311,6,data,true);
	appendBits(s106.var2024082319032012,7,data,true);

}
QString checkEncodeSeqNumber(S106 &s106)
{
	QString seqNum;
	int flag =0;
 
	int index =0; 
	 int count = 0;
	flag = 0;
	flag+=s106.ijsptbzhj!=0;
	flag+=s106.b0zt!=0;
	flag+=s106.boyxbs!=0;
	flag+=s106.bomusx!=0;
	flag+=s106.bosyhly!=44;
	flag+=s106.boby!=0;
	flag+=s106.bosyh!=0;
	flag+=s106.boias!=0;
	if( 0 == s106.boias){

	flag+=s106.jwh.idd!=0;
	}


	if( 1 == s106.boias){

	flag+=s106.bosd!=0;
	flag+=s106.boass!=0;
	}


	flag+=s106.boshjd!=0;
	flag+=s106.boxdsj!=0;
	if(flag != 0){seqNum+="3";}
	flag = 0;
	flag+=s106.bpby!=0;
	flag+=s106.ihx!=0;
	flag+=s106.isdu!=0;
	flag+=s106.bpdlzb!=1;
	if( 0 == s106.bpdlzb){

	flag+=s106.jwh.ijd!=0;
	flag+=s106.jwh.iwd!=0;
	}


	if( 1 == s106.bpdlzb){

	flag+=s106.bpwzlg!=0;
	flag+=s106.bp87!=0;
	flag+=s106.ibpx!=0;
	flag+=s106.bpyfhw!=0;
	flag+=s106.ibpy!=0;
	flag+=s106.ibpbyfl!=0;
	}


	if(flag != 0){seqNum+="11";}
	flag = 0;
	flag+=s106.bphjzl!=0;
	flag+=s106.imbsl!=0;
	flag+=s106.ihjlb!=0;
	flag+=s106.ibc1mylx!=1;
	if( 0 == s106.ibc1mylx){

	flag+=s106.bc1mblx!=0;
	flag+=s106.ibc1by!=0;
	}


	if( 1 == s106.ibc1mylx){

	flag+=s106.bc1mbxh!=0;
	}


	flag+=s106.bc1by!=0;
	flag+=s106.bc1mbbsh!=0;
	flag+=s106.bc1bzsh!=0;
	flag+=s106.bc1zsf!=0;
	if(flag != 0){seqNum+="21";}
	flag = 0;
	flag+=s106.bc2wfwqxt!=0;
	flag+=s106.bc2wqjzzt!=0;
	flag+=s106.bc2wmbh!=0;
	flag+=s106.bc2sysfzbh!=0;
	flag+=s106.bc2by!=0;
	flag+=s106.bc2wsfsyh!=0;
	flag+=s106.bc2by!=0;
	if(flag != 0){seqNum+="22";}
	flag = 0;
	flag+=s106.bc2cgqlx!=0;
	flag+=s106.bc3grjlx!=0;
	flag+=s106.bc3by!=0;
	flag+=s106.bc3rhlx!=0;
	flag+=s106.bc3mbbgjs!=0;
	flag+=s106.bc3fsyprf!=0;
	flag+=s106.bc3fsyzt!=0;
	flag+=s106.bc3sjgn!=0;
	flag+=s106.bc3sj!=0;
	flag+=s106.bc3fz!=0;
	flag+=s106.bc3m!=0;
	flag+=s106.bc3shm!=0;
	if(flag != 0){seqNum+="23";}
	flag = 0;
	flag+=s106.bc4by!=0;
	flag+=s106.bc4xjlwzbmq!=0;
	flag+=s106.bc4cpwzbm!=0;
	flag+=s106.bc4zzwzbmq!=0;
	flag+=s106.bc4fwjbqd!=0;
	flag+=s106.bc4qjbqd!=0;
	flag+=s106.bc4sdx!=0;
	flag+=s106.bc4sdbbqdd!=0;
	flag+=s106.bc4sddbqd!=0;
	flag+=s106.bc4sdxbqd!=0;
	if(flag != 0){seqNum+="24";}
	flag = 0;
	flag+=s106.bc5by!=0;
	flag+=s106.bc5wfbdgl!=0;
	flag+=s106.bc5dfbdgl!=0;
	flag+=s106.bc5mhxh1!=0;
	flag+=s106.bc5mhxh2!=0;
	flag+=s106.bc5mhxh1gl!=0;
	flag+=s106.bc5mhxh2gl!=0;
	if(flag != 0){seqNum+="25";}
	flag = 0;
	flag+=s106.bc6by!=0;
	flag+=s106.bc6jlsjmsf!=0;
	flag+=s106.bc6xs!=0;
	flag+=s106.bc6fzfssk!=0;
	flag+=s106.bc6m!=0;
	flag+=s106.bc6hm!=0;
	flag+=s106.bc6by!=0;
	flag+=s106.bc6fzjxsk!=0;
	flag+=s106.bc6msjdasd!=0;
	flag+=s106.bc6hmsjasidsd!=0;
	if(flag != 0){seqNum+="26";}
	flag = 0;
	flag+=s106.var202408231856592!=0;
	flag+=s106.var202408231859244!=0;
	flag+=s106.var202408231859475!=0;
	flag+=s106.var202408231900156!=0;
	flag+=s106.var202408231900407!=0;
	flag+=s106.var202408231901088!=0;
	flag+=s106.var202408231856592!=0;
	flag+=s106.var2024082319022610!=0;
	flag+=s106.var2024082319025311!=0;
	flag+=s106.var2024082319032012!=0;
	if(flag != 0){seqNum+="27";}
	 return seqNum;}

void VerifyField(S106 &s106)
{
	int flag = 0; 

	 flag = 0;
	 flag += (0 != s106.jwh.idd);
	if( 0 != flag )
		s106.boias=0;

	 flag = 0;
	 flag += (0 != s106.bosd);
	 flag += (0 != s106.boass);
	if( 0 != flag )
		s106.boias=1;

	 flag = 0;
	 flag += (0 != s106.jwh.ijd);
	 flag += (0 != s106.jwh.iwd);
	if( 0 != flag )
		s106.bpdlzb=0;

	 flag = 0;
	 flag += (0 != s106.bpwzlg);
	 flag += (0 != s106.bp87);
	 flag += (0 != s106.ibpx);
	 flag += (0 != s106.bpyfhw);
	 flag += (0 != s106.ibpy);
	 flag += (0 != s106.ibpbyfl);
	if( 0 != flag )
		s106.bpdlzb=1;

	 flag = 0;
	 flag += (0 != s106.bc1mblx);
	 flag += (0 != s106.ibc1by);
	if( 0 != flag )
		s106.ibc1mylx=0;

	 flag = 0;
	 flag += (0 != s106.bc1mbxh);
	if( 0 != flag )
		s106.ibc1mylx=1;
}
void updateFieldValue(S106 &s106)
{
}

void updateGroupFlag(S106 &s106)
{
}
void encodeMsg(QByteArray& data, S106 &s106){

	QString strSeqNum=checkEncodeSeqNumber(s106);
	QString temSeqNum;
	 temSeqNum="311212223242526";
	if(temSeqNum.contains(strSeqNum)){
	//Seq_1
		setverify1(s106,data);  
		return;
	}
	 temSeqNum="31121222324252627";
	if(temSeqNum.contains(strSeqNum)){
	//Seq_2
		setverify2(s106,data);  
		return;
	}
	writeSeq_1(s106,data);
}


